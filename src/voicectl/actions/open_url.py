from voicectl.actions._sys import sh
from voicectl.actions.base import Action, ActionResult, actions


@actions.register("open_url")
class OpenURL(Action):
    name = "open_url"
    description = "Open a website in the default browser."
    priority = 5
    patterns = [r"(?:go to|open|visit|navigate to) (?P<url>[\w-]+(?:\.[\w-]+)+(?:/\S*)?)"]
    params = {"url": "Domain or URL"}

    def run(self, url: str) -> ActionResult:
        full = url if "://" in url else f"https://{url}"
        r = sh(["open", full])
        host = full.split("//")[-1].split("/")[0].removeprefix("www.")
        return ActionResult(r.ok, f"Opened {full}" if r.ok else r.message, f"Opening {host}." if r.ok else "I couldn't open that.")
