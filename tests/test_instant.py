import pytest

from voicectl.modes.instant import match_instant, run_instant, search_url


@pytest.mark.parametrize("text,kind,arg", [
    ("scroll down", "scroll", "down"), ("Scroll up.", "scroll", "up"), ("please scroll down a bit", "scroll", "down"), ("page down", "scroll", "down"),
    ("scroll to the top", "scroll", "top"), ("go to the bottom of the page", "scroll", "bottom"), ("go back", "back", ""), ("back", "back", ""),
    ("go forward", "forward", ""), ("refresh the page", "reload", ""), ("new tab", "new_tab", ""), ("open a new tab", "new_tab", ""),
    ("close this tab", "close_tab", ""), ("switch to my LinkedIn tab", "switch_tab", "linkedin"), ("go to the apple jobs tab", "switch_tab", "apple jobs"),
    ("pause", "media", "pause"), ("pause the video", "media", "pause"), ("resume", "media", "play"), ("mute", "media", "mute"), ("unmute it", "media", "unmute"),
    ("search youtube for lofi beats", "search", "youtube|lofi beats"), ("search for ramen near me on google", "search", "google|ramen near me"),
])
def test_clear_browser_commands_are_recognised(text, kind, arg):
    c = match_instant(text)
    assert c is not None and c.kind == kind and c.arg == arg, (text, c)


@pytest.mark.parametrize("text", [
    "scroll down and find the apply button", "go back to the resume we were editing and fix the typo", "play some daft punk", "what is the back button for",
    "pause and think about it for a while and then tell me", "switch to the next tab", "switch to that tab", "search linkedin for jobs", "tell me a story",
    "open it in another tab", "close all the tabs", "back up my files",
])
def test_anything_else_goes_to_the_agent(text):
    assert match_instant(text) is None, text


class FakeBrowser:
    def __init__(self, page=True, tabs=True, fail=None):
        self.page, self.calls, self.fail, self.last_url = page, [], fail, "https://x.test/a"
        if tabs:
            self.switch_tab = self._rec("switch_tab"); self.new_tab = self._rec("new_tab"); self.close_tab = self._rec("close_tab")
    def has_page(self): return self.page
    def _rec(self, name):
        async def f(*a):
            self.calls.append((name, *a))
            if self.fail: raise self.fail
            return "ok"
        return f
    async def scroll(self, d, n): return await self._rec("scroll")(d, n)
    async def back(self): return await self._rec("back")()
    async def forward(self): return await self._rec("forward")()
    async def goto(self, url): return await self._rec("goto")(url)
    async def media(self, a): self.calls.append(("media", a)); return '{"playing": true}'


async def test_commands_run_on_the_browser_and_failures_raise_so_the_agent_can_take_over():
    b = FakeBrowser()
    await run_instant(match_instant("scroll down a bit"), b)
    await run_instant(match_instant("search youtube for lofi beats"), b)
    await run_instant(match_instant("switch to my linkedin tab"), b)
    assert b.calls == [("scroll", "down", 0.4), ("goto", search_url("youtube", "lofi beats")), ("switch_tab", "linkedin")]
    with pytest.raises(RuntimeError):
        await run_instant(match_instant("scroll down"), FakeBrowser(page=False))             # nothing open yet: the agent opens one
    with pytest.raises(RuntimeError):
        await run_instant(match_instant("new tab"), FakeBrowser(tabs=False))                  # no tab control on this backend
    with pytest.raises(RuntimeError):
        class NoMedia(FakeBrowser):
            async def media(self, a): return "NO_MEDIA"
        await run_instant(match_instant("pause"), NoMedia())
    assert search_url("google", "ramen & noodles") == "https://www.google.com/search?q=ramen+%26+noodles"
