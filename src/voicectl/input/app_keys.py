from __future__ import annotations

from typing import Any


def handle_key(data: dict[str, Any], name_to_mode, cancel_name, on_down, on_up, on_cancel) -> None:
    name, down = data.get("name"), bool(data.get("down"))
    if name == cancel_name:
        if down:
            on_cancel()
    elif name in name_to_mode:
        (on_down if down else on_up)(name_to_mode[name])
