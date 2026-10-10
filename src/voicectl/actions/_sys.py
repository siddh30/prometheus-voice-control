from __future__ import annotations

import subprocess

from voicectl.actions.base import ActionResult


def sh(cmd: list[str]) -> ActionResult:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        return ActionResult(True, "")
    return ActionResult(False, (p.stderr or p.stdout).strip() or f"exit {p.returncode}")


def osascript(script: str) -> ActionResult:
    return sh(["osascript", "-e", script])
