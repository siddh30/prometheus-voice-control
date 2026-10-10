from __future__ import annotations

import subprocess
from pathlib import Path

from voicectl.actions._sys import sh
from voicectl.actions.base import Action, ActionResult, actions

HOME = Path.home()
KNOWN = {"downloads": HOME / "Downloads", "documents": HOME / "Documents", "desktop": HOME / "Desktop",
         "home": HOME, "applications": Path("/Applications"), "pictures": HOME / "Pictures",
         "movies": HOME / "Movies", "music": HOME / "Music"}


def _spotlight(name: str) -> Path | None:
    out = subprocess.run(["mdfind", "-onlyin", str(HOME), f'kMDItemFSName == "{name}"c && kMDItemContentType == "public.folder"'],
                         capture_output=True, text=True).stdout.splitlines()
    out = [p for p in out if "/Library/" not in p and "/." not in p and "/node_modules/" not in p]
    return Path(min(out, key=len)) if out else None


@actions.register("open_folder")
class OpenFolder(Action):
    name = "open_folder"
    description = "Find a folder by name and open it in Finder (Downloads, Documents, or any folder name)."
    priority = 10
    patterns = [r"(?:open|show|find|go to)(?: me)?(?: the)? (?P<folder>[\w .-]+?) folder", r"open (?:my )?(?P<folder>downloads|documents|desktop|pictures|movies|music|applications)"]
    params = {"folder": "Folder name"}

    def run(self, folder: str) -> ActionResult:
        key = folder.lower().strip().removeprefix("my ")
        path = KNOWN.get(key) or _spotlight(folder.strip())
        if not path or not path.exists():
            return ActionResult(False, f"No folder named '{folder}'", f"I couldn't find a folder called {folder}.")
        r = sh(["open", str(path)])
        return ActionResult(r.ok, f"Opened {path}" if r.ok else r.message, f"Opened {path.name or folder}." if r.ok else "I couldn't open that folder.")
