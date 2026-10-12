from __future__ import annotations

from typing import Any

# One way to describe and check a message, used by every contract in the codebase: the front-end commands and events
# (api/protocol.py, api/events.py) and the swarm's controller <-> worker protocol (swarm/protocol.py).
# A table maps a message name to its fields; a field is (python type(s), required). An optional field may also be None.
Table = dict[str, dict[str, tuple[Any, bool]]]


def check(table: Table, name: Any, fields: dict[str, Any], what: str = "message") -> str | None:
    """None if `fields` honours the table's entry for `name`, else a one-line reason. Extra fields are allowed (a screenshot
    result carries its payload); a missing required field, a wrong type or an unknown name is not."""
    spec = table.get(name) if isinstance(name, str) else None
    if spec is None:
        return f"unknown {what} {name!r}"
    for key, (kind, required) in spec.items():
        if key not in fields:
            if required:
                return f"{name}: missing {key!r}"
        elif fields[key] is None and not required:
            continue
        elif not isinstance(fields[key], kind) or (kind is int and isinstance(fields[key], bool)):
            return f"{name}: {key!r} has the wrong type"
    return None
