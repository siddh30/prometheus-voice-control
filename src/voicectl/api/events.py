from __future__ import annotations

from typing import Any

# Daemon -> front end. Every message is {"type": <name>, "data": {...}}; this table is the whole contract.
# A field is (python type(s), required); an optional field may also be None. The widget ignores fields it doesn't know, so adding one is always safe.
EVENTS: dict[str, dict[str, tuple[Any, bool]]] = {
    "config":      {"hotkeys": (dict, True), "cancel": (str, True)},          # sent once on connect
    "heartbeat":   {},                                                         # every 3 s: the daemon is alive (the widget flags silence)
    "state":       {"state": (str, True), "mode": ((str, type(None)), False)},  # idle | listening | transcribing | thinking | acting ...
    "level":       {"value": ((int, float), True)},                            # microphone loudness 0..1
    "transcript":  {"text": (str, True), "mode": (str, False)},                # what you said or typed
    "notice":      {"text": (str, True)},                                      # a short heads-up
    "error":       {"message": (str, True)},
    "plan":        {"steps": (list, True)},
    "step":        {"text": (str, True), "done": (bool, False), "failed": (bool, False)},
    "checklist":   {"items": (list, True), "summary": (str, False), "level": ((str, int), False)},
    "result":      {"text": (str, True)},
    "speech_text": {"text": (str, True)},                                      # the words of a reply, as they are spoken
    "approval":    {"text": (str, True)},
    "job":         {"active": (bool, True), "text": (str, False)},
    "voice":       {"on": (bool, True)},
    "speaking":    {"on": (bool, True)},
    "music":       {"on": (bool, True), "title": (str, False)},
    "route":       {"kind": (str, True), "how": (str, False), "confidence": ((int, float), False), "action": (str, False)},
    "session":     {"label": (str, True)},
    "cost":        {"usd": ((int, float), True), "turns": (int, True)},
    "input_request": {"id": (str, True), "op": (str, True)},                   # the agent asks the app to look at / act on the screen
    "paste":       {"text": (str, True)},
}


def validate_event(type: str, data: dict[str, Any]) -> str | None:
    """None if the event honours the contract, else a one-line reason. Used by tests and by the bridge's log."""
    spec = EVENTS.get(type)
    if spec is None:
        return f"unknown event type {type!r}"
    for key, (kind, required) in spec.items():
        if key not in data:
            if required:
                return f"{type}: missing {key!r}"
        elif data[key] is None and not required:
            continue                                                           # an optional field may be left empty
        elif not isinstance(data[key], kind):
            return f"{type}: {key!r} has the wrong type"
    return None
