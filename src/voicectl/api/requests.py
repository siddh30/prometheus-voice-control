from __future__ import annotations

import asyncio
import uuid
from typing import Any

from voicectl.core.events import EventBus


class RequestTimeout(RuntimeError):
    pass


class RequestBroker:
    """Ask the front end something and wait for its answer: publish an event with a fresh id, then wait for the command
    that carries the same id back. The only place request/response over the event stream is implemented."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, event_type: str, timeout: float = 20.0, **args: Any) -> dict[str, Any]:
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.bus.publish(event_type, id=rid, **args)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RequestTimeout(event_type) from None
        finally:
            self._pending.pop(rid, None)

    def resolve(self, command) -> None:
        """Register as the handler of the answering command (Control.on("input_result", broker.resolve))."""
        fut = self._pending.pop(command.get("id", ""), None)
        if fut and not fut.done():
            fut.set_result(dict(command.args))
