from voicectl.actions._sys import sh
from voicectl.actions.base import Action, ActionResult, actions

ALIASES = {"browser": "Safari", "chrome": "Google Chrome", "vs code": "Visual Studio Code",
           "code": "Visual Studio Code", "terminal": "Terminal", "notes": "Notes", "mail": "Mail",
           "calendar": "Calendar", "music": "Music", "settings": "System Settings", "finder": "Finder"}

# "open LinkedIn" means the website, not an app called LinkedIn.
SITES = {"linkedin": "https://www.linkedin.com", "youtube": "https://www.youtube.com", "github": "https://github.com",
         "gmail": "https://mail.google.com", "google": "https://www.google.com", "reddit": "https://www.reddit.com",
         "twitter": "https://x.com", "x": "https://x.com", "facebook": "https://www.facebook.com",
         "instagram": "https://www.instagram.com", "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com",
         "google maps": "https://maps.google.com", "maps": "https://maps.google.com", "chatgpt": "https://chatgpt.com",
         "claude": "https://claude.ai", "indeed": "https://www.indeed.com", "glassdoor": "https://www.glassdoor.com"}


def _installed(name: str) -> bool:
    """Is there an application with this name on this Mac?"""
    from pathlib import Path
    names = {name, name.title(), name.replace(" ", "")}
    roots = [Path("/Applications"), Path("/System/Applications"), Path("/System/Applications/Utilities"),
             Path("/Applications/Utilities"), Path.home() / "Applications"]
    return any((root / f"{n}.app").exists() for root in roots for n in names)


@actions.register("open_app")
class OpenApp(Action):
    name = "open_app"
    description = "Open a macOS application by name (e.g. Safari, Notes, Slack), or a well-known website like LinkedIn or YouTube."
    patterns = [r"(?:open|launch|start)(?: up)?(?: (?:the|a|an|my))? (?P<app>[\w .+-]+?)(?: app| application| window)?"]
    params = {"app": "Application or website name"}

    @classmethod
    def match(cls, text: str) -> dict[str, str] | None:
        """Only claim "open X" when X is really an app or a known site. Anything else ("open Obvious by Chris Brown on
        YouTube") is a task for the agent, not a launcher command."""
        found = super().match(text)
        if not found:
            return None
        name = found["app"].strip()
        key = name.lower()
        if key in ALIASES or key in SITES or _installed(name):
            return found
        return None

    def run(self, app: str) -> ActionResult:
        key = app.lower().strip()
        if key in SITES:
            return self._site(app.strip().title() if key not in ("linkedin", "github", "youtube") else {"linkedin": "LinkedIn", "github": "GitHub", "youtube": "YouTube"}[key], SITES[key])
        name = ALIASES.get(key, app.title() if app.islower() else app)
        r = sh(["open", "-a", name])
        if r.ok:
            return ActionResult(True, f"Opened {name}", f"Opening {name}.")
        if " " not in key:                                   # not an app: try it as a website
            site = self._site(app.strip().title(), f"https://www.{key}.com")
            if site.ok:
                return site
        return ActionResult(False, f"Couldn't open {name}: {r.message}", f"I couldn't open {name}.")

    @staticmethod
    def _site(label: str, url: str) -> ActionResult:
        r = sh(["open", url])
        return ActionResult(r.ok, f"Opened {url}" if r.ok else r.message,
                            f"Opening {label}." if r.ok else f"I couldn't open {label}.")
