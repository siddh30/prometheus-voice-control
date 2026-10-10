from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voicectl.core.events import EventBus


async def route_app_keys(bus: EventBus, name_to_mode: dict[str, str], cancel_name: str,
                         on_down: Callable[[str], None], on_up: Callable[[str], None],
                         on_cancel: Callable[[], None]) -> None:
    """Translates {"type": "key", "name": "f9", "down": true} messages from the app into session calls."""
    async for ev in bus.subscribe():
        if ev.type != "ui_command" or ev.data.get("command") != "key":
            continue
        handle_key(ev.data, name_to_mode, cancel_name, on_down, on_up, on_cancel)


def handle_key(data: dict[str, Any], name_to_mode, cancel_name, on_down, on_up, on_cancel) -> None:
    name, down = data.get("name"), bool(data.get("down"))
    if name == cancel_name:
        if down:
            on_cancel()
    elif name in name_to_mode:
        (on_down if down else on_up)(name_to_mode[name])
