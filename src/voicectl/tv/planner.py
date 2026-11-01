"""TV control for sentences the plain parser does not know ("open the show I was watching on Netflix and start playing").

A small model fills in a plan form: a short list of steps, each one of a handful of kinds. It cannot write a command, name an
app that is not in the list, or press a key that is not in KEYS. `validate` checks every field in code and refuses the WHOLE
plan if anything is off, so the model can only ever choose among the same things the parser can.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote_plus

from voicectl import llm
from voicectl.tv.commands import DEFAULT_APPS, TvCommand

log = logging.getLogger(__name__)

KEYS = ("VOLUME_UP", "VOLUME_DOWN", "MUTE", "HOME", "BACK", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
        "DPAD_CENTER", "MEDIA_PLAY_PAUSE", "TV_INPUT")
OPS = ("key", "open_app", "volume_to", "wait", "youtube_search", "hdmi", "extend_display", "mirror_display", "stop_extending")
MAX_KEY_REPEAT = 10
MAX_WAIT_S = 15
MAX_TOTAL_WAIT_S = 25
MAX_QUERY = 80

SYSTEM = """You turn what someone says into a short plan for their TV remote. The TV can only do what the plan form allows:
press a remote key, open one of the listed apps, set the volume to a level, wait a few seconds, open a YouTube search, or switch HDMI input.
You cannot see the screen, search inside Netflix or other apps, or choose a specific show there.
Apps such as Netflix resume where the person stopped as soon as they open, so to resume or continue watching in an app, just open the app.
NEVER add a MEDIA_PLAY_PAUSE press after opening an app: it is a toggle and would pause what the app started by itself.
Use MEDIA_PLAY_PAUSE only when the person asks to pause, play or resume without asking to open an app.
Do not invent steps to reach a specific title. For YouTube, a search for what they asked is fine.
Three steps work on the person's computer, not the TV: mirror_display shows their main screen on the TV (the same picture), for requests
like "bring my work in", "bring the screen here", "put my desktop on the TV" or "mirror my screen"; extend_display makes the TV a separate
second screen instead, only when they say "extend";
stop_extending takes the screen away again and puts the TV back to normal, for "switch back to TV", "go back to normal TV" or "I am done".
Use them alone, as the only step.
Keep plans as short as possible (never more than the form allows). If the request cannot be done with these steps, return no steps.
Put one short, plain spoken sentence in `summary` saying what you will do."""


def tool_spec(app_names: list[str], max_steps: int) -> dict:
    return {
        "name": "tv_plan",
        "description": "The steps to send to the TV, in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One short spoken sentence: what you will do."},
                "steps": {
                    "type": "array", "maxItems": max_steps,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": list(OPS)},
                            "key": {"type": "string", "enum": list(KEYS), "description": "for op=key"},
                            "repeat": {"type": "integer", "minimum": 1, "maximum": MAX_KEY_REPEAT, "description": "for op=key: presses"},
                            "app": {"type": "string", "enum": sorted(app_names), "description": "for op=open_app"},
                            "level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "for op=volume_to"},
                            "seconds": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_S, "description": "for op=wait"},
                            "query": {"type": "string", "description": "for op=youtube_search"},
                            "port": {"type": "integer", "minimum": 1, "maximum": 4, "description": "for op=hdmi"},
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["steps"],
        },
    }


def _int(v: Any, lo: int, hi: int) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def validate(args: dict, apps: dict[str, str], inputs: dict[str, int] | None = None,
             max_steps: int = 6) -> tuple[list[TvCommand], str] | None:
    """The commands for a plan, plus its spoken summary, or None if any part of it is not allowed."""
    steps = args.get("steps") if isinstance(args, dict) else None
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        return None
    out: list[TvCommand] = []
    total_wait = 0.0
    for s in steps:
        if not isinstance(s, dict):
            return None
        op = s.get("op")
        if op == "key":
            key, repeat = s.get("key"), _int(s.get("repeat", 1), 1, MAX_KEY_REPEAT)
            if key not in KEYS or repeat is None:
                return None
            out.append(TvCommand("key", key, repeat=repeat, label=key.replace("_", " ").title()))
        elif op == "open_app":
            name = s.get("app")
            if not isinstance(name, str) or name.lower() not in apps:
                return None
            out.append(TvCommand("launch", package=apps[name.lower()], label=f"Opening {name.title()}"))
        elif op == "volume_to":
            level = _int(s.get("level"), 0, 100)
            if level is None:
                return None
            out.append(TvCommand("volume_to", repeat=level, label=f"Volume {level}"))
        elif op == "wait":
            sec = s.get("seconds")
            if isinstance(sec, bool) or not isinstance(sec, (int, float)) or not 1 <= sec <= MAX_WAIT_S:
                return None
            total_wait += sec
            if total_wait > MAX_TOTAL_WAIT_S:
                return None
            out.append(TvCommand("wait", repeat=round(sec)))
        elif op == "youtube_search":
            q = s.get("query")
            q = re.sub(r"[\x00-\x20]+", " ", q).strip() if isinstance(q, str) else ""        # control characters and runs of spaces collapse to one
            if not 1 <= len(q) <= MAX_QUERY:
                return None
            out.append(TvCommand("link", package="https://www.youtube.com/results?search_query=" + quote_plus(q),
                                 label="Searching YouTube"))
        elif op == "extend_display":
            if len(steps) != 1:
                return None
            out.append(TvCommand("extend_display", label="Bringing your screen to the TV"))
        elif op == "mirror_display":
            if len(steps) != 1:
                return None
            out.append(TvCommand("mirror_display", label="Putting your main screen on the TV"))
        elif op == "stop_extending":
            if len(steps) != 1:
                return None
            out.append(TvCommand("stop_extending", label="Switching back to the TV"))
        elif op == "hdmi":
            port = _int(s.get("port"), 1, 4)
            if port is None:
                return None
            out.append(TvCommand("key", f"TV_INPUT_HDMI_{port}", label=f"Switching to HDMI {port}"))
        else:
            return None
    out = _drop_toggle_after_launch(out)
    summary = args.get("summary")
    summary = re.sub(r"\s+", " ", summary).strip()[:140] if isinstance(summary, str) else ""
    return out, summary or ", then ".join(c.label for c in out if c.label)


def _drop_toggle_after_launch(cmds: list[TvCommand]) -> list[TvCommand]:
    """A play/pause press right after opening an app would pause what the app resumed by itself (Netflix does), so a model plan
    never keeps one. Waits that were only there to space it out go too."""
    out: list[TvCommand] = []
    for c in cmds:
        if c.kind == "key" and c.key == "MEDIA_PLAY_PAUSE":
            while out and out[-1].kind == "wait":
                out.pop()
            if out and out[-1].kind in ("launch", "link"):
                continue
        out.append(c)
    return out


def plan(text: str, apps: dict[str, str] | None = None, inputs: dict[str, int] | None = None,
         max_steps: int = 6, model: str = "claude-haiku-4-5") -> tuple[list[TvCommand], str] | None:
    """Ask the model for a plan for `text` and validate it. Blocking: call it with asyncio.to_thread. None when it cannot be done."""
    known = {**DEFAULT_APPS, **{k.lower(): v for k, v in (apps or {}).items()}}
    calls = llm.current().tool_calls(
        model=model, max_tokens=400, system=SYSTEM, messages=[{"role": "user", "content": text}],
        tools=[tool_spec(sorted(known), max_steps)], tool_choice={"type": "tool", "name": "tv_plan"})
    for call in calls:
        if call.name == "tv_plan":
            return validate(call.args, known, inputs, max_steps)
    return None
