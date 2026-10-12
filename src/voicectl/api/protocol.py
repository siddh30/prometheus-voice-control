from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Front end -> daemon. Every message is {"type": <name>, ...fields}; this table is the whole contract.
# A field is (python type, required). Extra fields are kept (e.g. a screenshot result carries its payload).
COMMANDS: dict[str, dict[str, tuple[type, bool]]] = {
    "text":         {"text": (str, True)},                       # a typed message: interrupts and becomes the newest instruction
    "input_mode":   {"mode": (str, True)},                       # "typed" (type box open: voice ignored) | "voice"
    "voice":        {"on": (bool, True)},                        # speak replies aloud, or text only
    "newchat":      {},                                          # fresh conversation, memory kept
    "fresh":        {},                                          # forget everything
    "maintenance":  {},                                          # tidy memory now
    "key":          {"name": (str, True), "down": (bool, True)}, # a hotkey seen by the app (it owns the macOS permissions)
    "approval":     {"approved": (bool, True)},                  # answer to an approval request
    "feedback":     {"rating": (str, True), "note": (str, False)},  # thumbs up / down on the latest run, with an optional note
    "input_result": {"id": (str, True)},                         # the app's answer to an input_request (screenshot, click, keys)
}

INPUT_MODES = ("typed", "voice")
RATINGS = ("up", "down")


@dataclass(frozen=True)
class Command:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.args[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)


def parse_command(message: dict[str, Any]) -> Command | None:
    """Validate one raw message; None means 'not a command we know', never an exception."""
    name = message.get("command") or message.get("type")
    spec = COMMANDS.get(name) if isinstance(name, str) else None
    if spec is None:
        return None
    args = {k: v for k, v in message.items() if k not in ("type", "command")}
    for key, (kind, required) in spec.items():
        if key not in args:
            if required:
                return None
            continue
        if not isinstance(args[key], kind):
            return None
    if name == "input_mode" and args["mode"] not in INPUT_MODES:
        return None
    if name == "feedback" and args["rating"] not in RATINGS:
        return None
    return Command(name, args)
