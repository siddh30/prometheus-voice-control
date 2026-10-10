from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    """Everything the UI/telemetry needs flows through these: type + free-form data."""
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Fan-out pub/sub. Slow subscribers drop their oldest event rather than block the core."""

    def __init__(self, maxsize: int = 256) -> None:
        self._subs: list[asyncio.Queue[Event]] = []
        self._maxsize = maxsize

    def publish(self, type: str, **data: Any) -> None:
        event = Event(type, data)
        for q in self._subs:
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subs.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.remove(q)
