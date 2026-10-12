from __future__ import annotations

import os
import re

from voicectl import llm
from voicectl.actions.base import Action, ActionResult, actions

SYSTEM = ("You map a spoken command to exactly one tool call for a macOS voice assistant. "
          "Pick the best-matching tool and fill its arguments from the command. "
          "If nothing fits, reply with plain text 'NONE' and call no tool.")


FAST_PATH_MAX_WORDS = 8          # short + a clean regex hit = run instantly, no model call

_LEAD = re.compile(r"^(?:(?:hey|okay|ok|so|um|uh|well|and|then|right|alright)[,\s]+)*"
                   r"(?:(?:can|could|would|will) you(?: please)?|please|i(?:'d| would) like you to|i want you to|go ahead and|let's)\s+", re.I)
_TAIL = re.compile(r"[,\s]*(?:for me|please|thanks|thank you|now|right now)\W*$", re.I)


def normalize(text: str) -> str:
    """Strip politeness so "Can you open LinkedIn for me, please?" becomes "open LinkedIn" for the instant path."""
    t = text.strip()
    for _ in range(2):
        t = _TAIL.sub("", _LEAD.sub("", t)).strip()
    return t


def quick_action(text: str) -> tuple[type[Action], dict[str, str]] | None:
    """The no-model path for exact one-step commands ("open linkedin", "can you open Safari please", "volume 30"): short, politeness
    stripped, and a clean pattern hit. None means a model (or the agent) must decide. Shared by single mode and the swarm worker."""
    plain = normalize(text)
    if len(plain.split()) <= FAST_PATH_MAX_WORDS and (hit := match_pattern(plain)):
        return hit
    return None


def match_pattern(

text: str) -> tuple[type[Action], dict[str, str]] | None:
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
