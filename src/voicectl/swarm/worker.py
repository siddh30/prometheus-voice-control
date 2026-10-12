from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from voicectl.swarm.protocol import COMMANDS, validate

log = logging.getLogger("voicectl.swarm.worker")

Send = Callable[[dict[str, Any]], Awaitable[None] | None]


class TaskContext:
    """What an executor gets for one task: what to do, a way to report progress, and a way to ask for approval."""

    def __init__(self, task_id: str, text: str, context: str, emit: Callable[..., None],
                 ask_approval: Callable[[str], Awaitable[bool]]) -> None:
        self.id, self.text, self.context = task_id, text, context
        self.emit = emit                        # emit("step", text="Opened Gmail", done=True): any worker event except done/failed
        self.ask_approval = ask_approval        # await ask_approval("Send this email?") -> True / False


class Executor(Protocol):
    """Does the work. The real one runs the normal Prometheus agent; tests use a scripted one."""

    async def run(self, task: TaskContext) -> str:
        """Do the task, reporting through task.emit. Returns the final answer text."""

    async def cancel(self, task_id: str) -> None:
        """Stop the task now (best effort)."""


@dataclass
class _Session:
    send: Send
    tasks: set[str] = field(default_factory=set)


@dataclass
class _Running:
    id: str
    session: _Session
    task: asyncio.Task | None = None
    last_activity: float = 0.0
    stalled: bool = False
    approval: asyncio.Future | None = None


class Worker:
    """One computer's side of the swarm: runs tasks for a controller, with a monitor beside them.
    The monitor is the thing that notices a task has gone quiet and says so; it never touches the task itself."""

    def __init__(self, name: str, executor: Executor, *, stall_after_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.executor, self.stall_after_s, self._clock = name, executor, stall_after_s, clock
        self._running: dict[str, _Running] = {}
        self._monitor: asyncio.Task | None = None

    # ---- connections ----
    def attach(self, send: Send) -> _Session:
        return _Session(send)

    async def detach(self, session: _Session) -> None:
        """The controller went away: nobody is left to report to, so its tasks stop."""
        for task_id in list(session.tasks):
            await self._cancel(task_id, report=False)

    # ---- commands from the controller ----
    async def command(self, session: _Session, message: dict[str, Any]) -> None:
        problem = validate(COMMANDS, message)
        if problem:
            log.warning("ignoring bad command: %s", problem)
            return
        kind = message["type"]
        if kind == "task":
            self._start(session, message["id"], message["text"], message.get("context") or "")
        elif kind == "cancel":
            await self._cancel(message["id"], report=True)
        elif kind == "approval":
            run = self._running.get(message["id"])
            if run and run.approval and not run.approval.done():
                run.approval.set_result(bool(message["approved"]))

    # ---- one task ----
    def _start(self, session: _Session, task_id: str, text: str, context: str) -> None:
        if task_id in self._running:
            return                                           # a repeated request must not run twice
        run = _Running(task_id, session, last_activity=self._clock())
        self._running[task_id] = run
        session.tasks.add(task_id)
        run.task = asyncio.get_running_loop().create_task(self._run(run, text, context))

    async def _run(self, run: _Running, text: str, context: str) -> None:
        outbox: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def pump() -> None:                            # the only sender for this task: events leave in the order they happened
            while (item := await outbox.get()) is not None:
                kind, fields = item
                run.last_activity = self._clock()
                if run.stalled:
                    run.stalled = False
                    await self._send(run.session, {"type": "resumed", "id": run.id})
                await self._send(run.session, {"type": kind, "id": run.id, **fields})

        def emit(kind: str, **fields: Any) -> None:
            run.last_activity = self._clock()                 # activity counts the moment it happens, not when it is sent
            outbox.put_nowait((kind, fields))

        async def ask_approval(question: str) -> bool:
            run.approval = asyncio.get_running_loop().create_future()
            emit("approval", text=question)
            try:
                return await run.approval
            finally:
                run.approval = None

        sender = asyncio.get_running_loop().create_task(pump())
        try:
            emit("started")
            final = await self.executor.run(TaskContext(run.id, text, context, emit, ask_approval))
            if final:
                emit("result", text=final)
            outbox.put_nowait(None)
            await sender                                      # everything reported before "done"
            await self._send(run.session, {"type": "done", "id": run.id})
        except asyncio.CancelledError:
            sender.cancel()
            raise
        except Exception as e:
            log.exception("task %s failed", run.id)
            outbox.put_nowait(None)
            await asyncio.gather(sender, return_exceptions=True)
            await self._send(run.session, {"type": "failed", "id": run.id, "error": f"{type(e).__name__}: {e}"})
        finally:
            self._running.pop(run.id, None)
            run.session.tasks.discard(run.id)

    async def _cancel(self, task_id: str, *, report: bool) -> None:
        run = self._running.get(task_id)
        if not run or not run.task:
            return
        try:
            await asyncio.wait_for(self.executor.cancel(task_id), 3)
        except Exception:
            log.debug("executor cancel failed for %s", task_id, exc_info=True)
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)
        if report:
            await self._send(run.session, {"type": "failed", "id": task_id, "error": "cancelled"})

    # ---- the monitor ----
    async def check_stalls(self) -> None:
        """Tell the controller about any task that is running but has gone quiet. Safe to call as often as you like."""
        now = self._clock()
        for run in list(self._running.values()):
            quiet = now - run.last_activity
            if not run.stalled and quiet >= self.stall_after_s and not (run.approval and not run.approval.done()):
                run.stalled = True                          # (waiting for a human's yes/no is not a stall)
                await self._send(run.session, {"type": "stalled", "id": run.id, "seconds": round(quiet, 1)})

    async def run_monitor(self, interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_stalls()
            except Exception:
                log.exception("monitor tick failed")

    def start_monitor(self, interval: float = 1.0) -> None:
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.get_running_loop().create_task(self.run_monitor(interval))

    async def stop(self) -> None:
        if self._monitor:
            self._monitor.cancel()
        for task_id in list(self._running):
            await self._cancel(task_id, report=False)

    # ---- plumbing ----
    @staticmethod
    async def _send(session: _Session, message: dict[str, Any]) -> None:
        try:
            result = session.send(message)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.debug("could not send %s (connection closed?)", message.get("type"))
