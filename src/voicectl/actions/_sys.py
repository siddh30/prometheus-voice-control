from __future__ import annotations

from voicectl import platform
from voicectl.actions.base import ActionResult


def sh(cmd: list[str]) -> ActionResult:
    p = platform.current().run(cmd)
    if p.returncode == 0:
        return ActionResult(True, "")
    return ActionResult(False, (p.stderr or p.stdout).strip() or f"exit {p.returncode}")


def osascript(script: str) -> ActionResult:
    r = platform.current().osascript(script)
    return ActionResult(True, "") if r.returncode == 0 else ActionResult(False, (r.stderr or r.stdout).strip() or f"exit {r.returncode}")
