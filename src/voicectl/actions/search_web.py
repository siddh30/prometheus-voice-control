import re
from urllib.parse import quote_plus

from voicectl.actions._sys import sh
from voicectl.actions.base import Action, ActionResult, actions


@actions.register("search_web")
class SearchWeb(Action):
    name = "search_web"
    description = "Search Google for a query in the default browser."
    priority = 5
    patterns = [r"(?:google|search(?: google| the web| online)?(?: for)?|look up) (?P<query>.+)"]
    params = {"query": "What to search for"}

    SITE_WORDS = re.compile(r"\b(youtube|linkedin|amazon|netflix|spotify|reddit|github|twitter|facebook|instagram|maps|gmail|"
                            r"indeed|glassdoor|chatgpt|claude|wikipedia|ebay|etsy|walmart|target)\b", re.I)

    @classmethod
    def match(cls, text: str) -> dict[str, str] | None:
        """A plain Google search only. "Search YouTube for ..." or "find jobs on LinkedIn" belong to the agent, which
        searches that site itself."""
        found = super().match(text)
        if found and cls.SITE_WORDS.search(found["query"]):
            return None
        return found

    def run(self, query: str) -> ActionResult:
        r = sh(["open", f"https://www.google.com/search?q={quote_plus(query)}"])
        return ActionResult(r.ok, f"Searching: {query}" if r.ok else r.message, f"Searching for {query}." if r.ok else "I couldn't search.")
