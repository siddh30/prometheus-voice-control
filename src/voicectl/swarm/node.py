from __future__ import annotations

"""A swarm node: this computer's side of Prometheus Swarm. It dials the hub (so it works wherever the Mac is, however its address
changes), says who it is and what it is good for, then runs the tasks it is given with its own agent and reports progress."""

import asyncio
import json
import logging
import platform
from collections.abc import Awaitable, Callable
from typing import Protocol

from websockets.asyncio.client import connect

log = logging.getLogger("voicectl.swarm.node")

Emit = Callable[..., Awaitable[None]]


class Executor(Protocol):
    async def run(self, text: str, emit: Emit) -> str: ...
    async def cancel(self) -> None: ...
    def approve(self, ok: bool) -> None: ...


class SwarmNode:
    def __init__(self, name: str, hub_url: str, token: str, executor: Executor, description: str = "", caps: list[str] | None = None,
                 retry_s: tuple[float, float] = (1.0, 15.0)) -> None:
        self.name, self.executor, self.description, self.caps = name, executor, description, caps or []
        sep = "&" if "?" in hub_url else "?"
        self.url = f"{hub_url.rstrip('/')}/node{sep}token={token}" if token else f"{hub_url.rstrip('/')}/node"
        self.retry_s = retry_s
        self.connected = asyncio.Event()
        self._ws = None
        self._task: asyncio.Task | None = None
        self._sub = ""

    async def _send(self, msg: dict) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                log.debug("couldn't send to the hub: %s", e)

    async def run(self) -> None:
        delay = self.retry_s[0]
        while True:
            try:
                async with connect(self.url, max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=30) as ws:
                    self._ws, delay = ws, self.retry_s[0]
                    await ws.send(json.dumps({"type": "hello", "name": self.name, "description": self.description, "caps": self.caps,
                                              "os": f"{platform.system()} {platform.machine()}"}))
                    self.connected.set()
                    log.info("joined the swarm as %s", self.name)
                    beat = asyncio.create_task(self._heartbeat())
                    try:
                        async for raw in ws:
                            await self._on_message(raw)
                    finally:
                        beat.cancel()
            except asyncio.CancelledError:
                await self._abort_task()
                raise
            except Exception as e:
                log.info("swarm hub unreachable (%s); retrying in %.0fs", type(e).__name__, delay)
            self._ws = None
            self.connected.clear()
            await self._abort_task()                       # the hub has already written this task off
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry_s[1])

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self._send({"type": "heartbeat"})

    async def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "task":
            if self._task and not self._task.done():       # one task at a time: the hub only sends to an idle node
                await self._send({"type": "done", "sub": msg.get("sub"), "ok": False, "result": f"{self.name} is already busy."})
                return
            self._sub = str(msg.get("sub", ""))
            self._task = asyncio.create_task(self._run_task(self._sub, str(msg.get("text", ""))))
        elif kind == "cancel" and str(msg.get("sub", "")) == self._sub:
            await self._abort_task()
        elif kind == "approve":
            self.executor.approve(bool(msg.get("ok")))

    async def _run_task(self, sub: str, text: str) -> None:
        async def emit(kind: str, **data) -> None:
            await self._send({"type": kind, "sub": sub, **data})
        try:
            result = await self.executor.run(text, emit)
            await self._send({"type": "done", "sub": sub, "ok": True, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as e:                              # an agent error must never take the node down
            log.exception("swarm task failed")
            await self._send({"type": "done", "sub": sub, "ok": False, "result": f"{type(e).__name__}: {e}"})

    async def _abort_task(self) -> None:
        t, self._task = self._task, None
        if t and not t.done():
            t.cancel()
            try:
                await self.executor.cancel()
            except Exception:
                pass
            await asyncio.gather(t, return_exceptions=True)


class AgentExecutor:
    """Runs a swarm task with this Mac's Prometheus agent, in its OWN conversation (private session and memory), so a task from the
    swarm never mixes with whatever you are doing here. It shares this machine's browser connection and screen control."""

    HINT = ("<SwarmTask>This task was assigned by your Prometheus Swarm hub to run on THIS computer. Do it with your tools, and finish "
            "with a short plain-text result. Nobody is listening: do not address the user by voice; keep any narration to one sentence."
            "</SwarmTask>")

    def __init__(self, config, primary_runner_getter, data_dir) -> None:
        self.config, self._primary, self.data_dir = config, primary_runner_getter, data_dir
        self._runner = None

    def _ensure(self):
        if self._runner is None:
            from voicectl.agents.runner import AgentRunner
            from voicectl.core.events import EventBus
            from voicectl.core.memory import Memory
            primary = self._primary()
            self._runner = AgentRunner(self.config, EventBus(), Memory(self.data_dir / "swarm_node.db"),
                                       browser=getattr(primary, "browser", None), screen=getattr(primary, "screen", None),
                                       prompt_name="prometheus_prompt.txt", swarm_tools=False)
            self._runner.mode_name = "swarm"
        return self._runner

    async def run(self, text: str, emit: Emit) -> str:
        from voicectl.agents.complexity import heuristic, hint_block
        from voicectl.agents.runner import Finished
        runner = self._ensure()
        c = heuristic(text)

        async def forward() -> None:
            async for ev in runner.bus.subscribe():
                try:
                    if ev.type == "step":
                        await emit("step", text=ev.data.get("text", ""), done=bool(ev.data.get("done")), failed=bool(ev.data.get("failed")))
                    elif ev.type == "checklist":
                        await emit("checklist", items=ev.data.get("items", []))
                    elif ev.type == "approval":
                        await emit("approval", text=ev.data.get("text", "Okay to continue?"))
                except Exception:
                    pass
        fwd = asyncio.create_task(forward())
        final = ""
        try:
            async for ev in runner.stream_turn(text, "", f"{self.HINT}\n{hint_block(c)}", c.level, publish_result=False):
                if isinstance(ev, Finished):
                    final = ev.text
        finally:
            fwd.cancel()
        return final.strip() or "Done."

    async def cancel(self) -> None:
        if self._runner:
            await self._runner.interrupt()

    def approve(self, ok: bool) -> None:
        if self._runner:
            self._runner.on_ui_command({"command": "approval", "approved": ok})
