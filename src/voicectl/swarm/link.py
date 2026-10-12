from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from websockets.asyncio.client import connect

from voicectl.swarm.protocol import EVENTS, PROTOCOL_VERSION, validate
from voicectl.swarm.worker import Worker

log = logging.getLogger("voicectl.swarm.link")


class LinkState(StrEnum):
    CONNECTING = "connecting"
    ONLINE = "online"
    NOT_RESPONDING = "not_responding"      # connected, but the worker has said nothing for a while (heartbeats stopped)
    OFFLINE = "offline"                    # not connected (refused, dropped, wrong token)


class LinkUnavailable(RuntimeError):
    pass


EventHandler = Callable[[str, dict[str, Any]], None]
StateHandler = Callable[[str, LinkState], None]


class Link:
    """One worker, as the controller sees it. Everything here is time-boxed: nothing ever waits on a computer that has gone quiet,
    and a change of state is reported as it happens (`on_state`) instead of being discovered by whoever asks."""

    name: str
    state: LinkState = LinkState.CONNECTING
    detail: str = ""                        # why it is offline, in plain words
    on_event: EventHandler | None = None
    on_state: StateHandler | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_task(self, task_id: str, text: str, context: str = "") -> None: ...
    async def cancel(self, task_id: str) -> None: ...
    async def approve(self, task_id: str, approved: bool) -> None: ...

    def _set(self, state: LinkState, detail: str = "") -> None:
        if state != self.state or detail != self.detail:
            self.state, self.detail = state, detail
            if self.on_state:
                try:
                    self.on_state(self.name, state)
                except Exception:
                    log.exception("state handler failed")

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(self.name, event)
            except Exception:
                log.exception("event handler failed")


class LocalLink(Link):
    """This PC's own worker, in the same process: identical behaviour to a remote one, with no network in between."""

    def __init__(self, name: str, worker: Worker) -> None:
        self.name, self.worker = name, worker
        self.state = LinkState.ONLINE
        self._session = worker.attach(self._receive)

    def _receive(self, message: dict[str, Any]) -> None:
        if message.get("type") != "heartbeat":
            self._emit(message)

    async def start(self) -> None:
        self.worker.start_monitor()

    async def stop(self) -> None:
        await self.worker.stop()

    async def send_task(self, task_id: str, text: str, context: str = "") -> None:
        await self.worker.command(self._session, {"type": "task", "id": task_id, "text": text, "context": context})

    async def cancel(self, task_id: str) -> None:
        await self.worker.command(self._session, {"type": "cancel", "id": task_id})

    async def approve(self, task_id: str, approved: bool) -> None:
        await self.worker.command(self._session, {"type": "approval", "id": task_id, "approved": approved})


class RemoteLink(Link):
    def __init__(self, name: str, host: str, port: int, token: str, *, silent_after_s: float = 8.0, connect_timeout_s: float = 3.0,
                 max_backoff_s: float = 10.0, retry_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.host, self.port, self.token = name, host, port, token
        self.silent_after_s, self.connect_timeout_s, self.max_backoff_s, self.retry_s, self._clock = silent_after_s, connect_timeout_s, max_backoff_s, retry_s, clock
        self.state = LinkState.CONNECTING
        self._ws = None
        self._heard = 0.0
        self._runner: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None

    async def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.get_running_loop().create_task(self._run())
            self._watchdog = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        for t in (self._runner, self._watchdog):
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (self._runner, self._watchdog) if t), return_exceptions=True)
        self._runner = self._watchdog = None
        self._set(LinkState.OFFLINE, "stopped")

    # ---- connection loop ----
    async def _run(self) -> None:
        backoff = self.retry_s
        while True:
            self._set(LinkState.CONNECTING)
            ws = None
            try:
                async with asyncio.timeout(self.connect_timeout_s * 2):
                    ws = await connect(f"ws://{self.host}:{self.port}", open_timeout=self.connect_timeout_s, max_size=1024 * 1024)
                    await ws.send(json.dumps({"type": "hello", "token": self.token, "controller": "swarm", "version": PROTOCOL_VERSION}))
                    welcome = json.loads(await ws.recv())
                if welcome.get("type") != "welcome":
                    await ws.close()
                    self._set(LinkState.OFFLINE, "that computer refused the shared token")
                    backoff = self.max_backoff_s
                else:
                    self._ws, self._heard, backoff = ws, self._clock(), self.retry_s
                    self._set(LinkState.ONLINE)
                    await self._read(ws)
                    self._set(LinkState.OFFLINE, "the connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._set(LinkState.OFFLINE, f"can't reach {self.host}:{self.port}" if isinstance(e, (OSError, TimeoutError))
                          else f"{type(e).__name__}")
            finally:
                self._ws = None
                if ws is not None:                              # always hang up: the worker stops our tasks when we leave
                    try:
                        await asyncio.shield(ws.close())
                    except Exception:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_s)

    async def _read(self, ws) -> None:
        async for raw in ws:
            self._heard = self._clock()
            if self.state is LinkState.NOT_RESPONDING:
                self._set(LinkState.ONLINE)
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("type") != "heartbeat" and validate(EVENTS, event) is None:
                self._emit(event)

    async def _watch(self) -> None:
        """The controller's own stopwatch: if the worker's heartbeats stop, say so, whatever the worker thinks it is doing."""
        while True:
            await asyncio.sleep(max(0.05, min(1.0, self.silent_after_s / 4)))
            if self.state is LinkState.ONLINE and self._clock() - self._heard >= self.silent_after_s:
                self._set(LinkState.NOT_RESPONDING, "no word from that computer")

    # ---- commands ----
    async def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or self.state in (LinkState.OFFLINE, LinkState.CONNECTING):
            raise LinkUnavailable(f"{self.name} is {self.state.value}")
        try:
            await asyncio.wait_for(ws.send(json.dumps(message)), self.connect_timeout_s)
        except Exception as e:
            raise LinkUnavailable(f"could not reach {self.name}: {type(e).__name__}") from e

    async def send_task(self, task_id: str, text: str, context: str = "") -> None:
        await self._send({"type": "task", "id": task_id, "text": text, "context": context})

    async def cancel(self, task_id: str) -> None:
        await self._send({"type": "cancel", "id": task_id})

    async def approve(self, task_id: str, approved: bool) -> None:
        await self._send({"type": "approval", "id": task_id, "approved": approved})
