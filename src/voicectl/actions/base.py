from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from voicectl.core.registry import Registry


@dataclass
class ActionResult:
    ok: bool
    message: str            # shown on screen
    spoken: str = ""        # short form read aloud (falls back to a cleaned `message`)


class Action(ABC):
    """A simple OS action. Subclass, set the class attributes, register with @actions.register(name).

    patterns: regexes (matched case-insensitively against the whole transcript) whose named groups
              become `args`. params: {arg: description} so the LLM fallback can fill them in.
    """
    name: str
    description: str
    patterns: list[str] = []
    priority: int = 0              # higher wins when several actions' patterns match the same words
    params: dict[str, str] = {}

    @abstractmethod
    def run(self, **args: str) -> ActionResult: ...

    @classmethod
    def match(cls, text: str) -> dict[str, str] | None:
        cleaned = text.strip().rstrip(".!?").strip()
        for pat in cls.patterns:
            m = re.fullmatch(pat, cleaned, flags=re.IGNORECASE)
            if m:
                return {k: v.strip() for k, v in m.groupdict().items() if v}
        return None

    @classmethod
    def spec(cls) -> dict:
        """JSON-schema tool spec used by the LLM fallback."""
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": {
                "type": "object",
                "properties": {k: {"type": "string", "description": d} for k, d in cls.params.items()},
                "required": list(cls.params),
            },
        }


actions: Registry[Action] = Registry("action")
