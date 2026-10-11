from __future__ import annotations

import os

from voicectl import llm
from voicectl.actions.base import Action, ActionResult, actions

SYSTEM = ("You map a spoken command to exactly one tool call for a macOS voice assistant. "
          "Pick the best-matching tool and fill its arguments from the command. "
          "If nothing fits, reply with plain text 'NONE' and call no tool.")


def match_pattern(text: str) -> tuple[type[Action], dict[str, str]] | None:
    """Tier 1: regex patterns declared on each action. Instant, no network. Most specific (longest) pattern first."""
    for cls in sorted(actions.all().values(), key=lambda c: (-c.priority, -max((len(p) for p in c.patterns), default=0))):
        args = cls.match(text)
        if args is not None:
            return cls, args
    return None


def match_llm(text: str, model: str = "claude-sonnet-4-6") -> tuple[type[Action], dict[str, str]] | None:
    """Tier 2: Sonnet picks an action + args from the registry's schemas."""
    calls = llm.current().tool_calls(model=model, max_tokens=300, system=SYSTEM,
                                     tools=[c.spec() for c in actions.all().values()], tool_choice={"type": "auto"},
                                     messages=[{"role": "user", "content": text}])
    for c in calls:
        if c.name in actions.all():
            return actions.get(c.name), {k: str(v) for k, v in c.args.items()}
    return None


def run_action(text: str, use_llm: bool = True) -> ActionResult:
    found = match_pattern(text) or (match_llm(text) if use_llm else None)
    if not found:
        return ActionResult(False, f"I don't know how to: {text}")
    cls, args = found
    return cls().run(**args)
