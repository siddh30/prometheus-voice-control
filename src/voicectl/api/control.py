from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable

from voicectl.api.protocol import Command, parse_command
from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.api")


class Control:
    """The one place front-end commands arrive. Register a handler per command; each message is validated first,
    and a failing handler is logged, never allowed to stop the others or the loop."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._handlers: dict[str, list[Callable]] = {}
        self._locks: dict[str, asyncio.Lock] = {}              # one command's handlers run in order, but never hold up another command
        self._tasks: set[asyncio.Task] = set()
        self._runner: asyncio.Task | None = None

    def on(self, name: str, handler: Callable[[Command], object] | None = None):
        """control.on("voice", fn)  or  @control.on("voice")  -- fn may be sync or async and takes the Command."""
        def register(fn: Callable) -> Callable:
            self._handlers.setdefault(name, []).append(fn)
            return fn
        return register(handler) if handler else register

    def start(self) -> None:
        """Begin listening (idempotent: every part of the daemon may ask for it)."""
        if self._runner is None or self._runner.done():
            self._runner = asyncio.get_running_loop().create_task(self.run())

    def stop(self) -> None:
        if self._runner:
            self._runner.cancel()

    async def dispatch(self, command: Command) -> None:
        lock = self._locks.setdefault(command.name, asyncio.Lock())
        async with lock:
            for fn in self._handlers.get(command.name, []):
                try:
                    result = fn(command)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    log.exception("handler for %r failed", command.name)

    async def run(self) -> None:
        async for ev in self.bus.subscribe():
            if ev.type != "ui_command":
                continue
            command = parse_command(ev.data)
            if command is None:
                log.warning("ignoring unknown or malformed command: %s", {k: v for k, v in ev.data.items() if k != "text"})
                continue
            task = asyncio.create_task(self.dispatch(command))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
