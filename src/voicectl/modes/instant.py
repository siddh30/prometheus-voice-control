from __future__ import annotations

"""Instant browser commands: the small, unambiguous things people say all day ("scroll down", "go back", "pause", "new tab",
"switch to my LinkedIn tab", "search YouTube for lofi") run straight on the browser, with no model in the loop (about 0.3 s instead
of 5 s or more).

Deliberately conservative: only whole-utterance patterns match; anything else, and any failure, goes to the agent, which can do
everything these can. Turn the whole thing off with `agent.instant_browser: false`."""

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

SEARCH_SITES = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
}


@dataclass(frozen=True)
class Instant:
    kind: str                  # scroll | back | forward | reload | new_tab | close_tab | switch_tab | media | search
    arg: str = ""
    amount: float = 1.0
    spoken: str = "Done."
    needs_tabs: bool = False   # only the DevTools backend can do tab operations


def _clean(text: str) -> str:
    t = re.sub(r"[^\w\s'&]", " ", text.lower())
    t = re.sub(r"^(?:ok(?:ay)?|hey|please|prometheus|can you|could you|just)\s+", "", t.strip())
    t = re.sub(r"\s+(?:please|now|for me|again|thanks)$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


_SCROLL = re.compile(r"^(?:scroll|go|page|move)\s+(down|up)(?:\s+(?:a\s+)?(little|bit|little bit|lot|page|screen|more))?(?:\s+(?:a\s+)?(little|bit|more))?$")
_PAGE = re.compile(r"^page\s+(down|up)$")
_EDGE = re.compile(r"^(?:scroll|go|jump)\s+(?:to\s+)?(?:the\s+)?(top|bottom)(?:\s+of\s+(?:the\s+)?page)?$")
_BACK = re.compile(r"^(?:go\s+)?back$|^(?:go\s+)?(?:to\s+the\s+)?previous page$")
_FWD = re.compile(r"^go\s+forward$|^forward$")
_RELOAD = re.compile(r"^(?:reload|refresh)(?:\s+(?:the\s+|this\s+)?page)?$")
_NEWTAB = re.compile(r"^(?:open\s+)?(?:a\s+)?new\s+tab$")
_CLOSETAB = re.compile(r"^close\s+(?:this|the|that|current)\s+tab$|^close\s+tab$")
_SWITCH = re.compile(r"^(?:switch|go)\s+to\s+(?:my\s+|the\s+)?(.{2,40}?)\s+tab$")
_MEDIA = {
    "pause": re.compile(r"^pause(?:\s+(?:it|the\s+(?:video|music|song|audio)))?$|^stop\s+the\s+(?:video|music|song)$"),
    "play": re.compile(r"^(?:resume|continue|unpause)(?:\s+(?:it|the\s+(?:video|music|song|audio)))?$|^play$|^play\s+(?:it|the\s+(?:video|music|song))$"),
    "mute": re.compile(r"^mute(?:\s+(?:it|the\s+(?:video|music|song|tab|audio)))?$"),
    "unmute": re.compile(r"^unmute(?:\s+(?:it|the\s+(?:video|music|song|tab|audio)))?$"),
}
_MEDIA_SPOKEN = {"pause": "Paused.", "play": "Playing.", "mute": "Muted.", "unmute": "Unmuted."}
_SEARCH1 = re.compile(r"^search\s+(?:for\s+)?(.{2,80}?)\s+on\s+(youtube|google maps|google|amazon|maps|wikipedia)$")
_SEARCH2 = re.compile(r"^(?:search\s+)?(youtube|google maps|google|amazon|wikipedia)\s+for\s+(.{2,80})$")


def search_url(site: str, query: str) -> str:
    return SEARCH_SITES[site].format(q=quote_plus(query))


def match_instant(text: str) -> Instant | None:
    """The instant command this utterance is, or None (the agent decides). Whole-utterance matches only."""
    t = _clean(text)
    if not t or len(t.split()) > 9:
        return None
    if m := _PAGE.match(t):
        return Instant("scroll", m.group(1), 1.0, "Scrolling.")
    if m := _EDGE.match(t):
        return Instant("scroll", m.group(1), 1.0, "Scrolling.")
    if m := _SCROLL.match(t):
        qualifier = (m.group(2) or m.group(3) or "")
        amount = 0.4 if qualifier in ("little", "bit", "little bit") else 2.0 if qualifier == "lot" else 1.0
        return Instant("scroll", m.group(1), amount, "Scrolling.")
    if _BACK.match(t):
        return Instant("back", spoken="Going back.")
    if _FWD.match(t):
        return Instant("forward", spoken="Going forward.")
    if _RELOAD.match(t):
        return Instant("reload", spoken="Reloading.")
    if _NEWTAB.match(t):
        return Instant("new_tab", spoken="New tab.", needs_tabs=True)
    if _CLOSETAB.match(t):
        return Instant("close_tab", spoken="Closing the tab.", needs_tabs=True)
    for action, rx in _MEDIA.items():
        if rx.match(t):
            return Instant("media", action, spoken=_MEDIA_SPOKEN[action])
    if m := _SWITCH.match(t):
        name = m.group(1)
        if name not in {"next", "previous", "last", "new", "another", "other", "this", "that", "a", "the"}:
            return Instant("switch_tab", name, spoken=f"Switching to {name}.", needs_tabs=True)
    if m := _SEARCH1.match(t):
        return Instant("search", f"{m.group(2)}|{m.group(1)}", spoken=f"Searching {m.group(2).title()}.")
    if m := _SEARCH2.match(t):
        return Instant("search", f"{m.group(1)}|{m.group(2)}", spoken=f"Searching {m.group(1).title()}.")
    return None


async def run_instant(cmd: Instant, browser) -> None:
    """Do it on `browser`. Raises on any failure so the caller can hand the request to the agent."""
    if cmd.needs_tabs and not hasattr(browser, "switch_tab"):
        raise RuntimeError("this browser backend has no tab control")
    if not browser.has_page() and cmd.kind not in ("new_tab", "search"):
        raise RuntimeError("no page is open yet")
    match cmd.kind:
        case "scroll":
            await browser.scroll(cmd.arg if cmd.arg in ("top", "bottom") else cmd.arg, cmd.amount)
        case "back":
            await browser.back()
        case "forward":
            await browser.forward()
        case "reload":
            await browser.goto(browser.last_url)
        case "new_tab":
            await browser.new_tab("about:blank")
        case "close_tab":
            await browser.close_tab("")
        case "switch_tab":
            await browser.switch_tab(cmd.arg)
        case "media":
            r = await browser.media(cmd.arg)
            if r in ("NO_MEDIA", "BAD_ACTION") or str(r).startswith("BLOCKED"):
                raise RuntimeError(f"media: {r}")
        case "search":
            site, query = cmd.arg.split("|", 1)
            await browser.goto(search_url(site, query))
        case _:
            raise RuntimeError(f"unknown instant command {cmd.kind}")
