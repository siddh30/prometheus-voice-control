import pytest

from voicectl.agents.devtools import DevToolsBrowser, DevToolsClient, DevToolsError, Result, SETUP_HELP, parse_snapshot, strip_page_list

SNAPSHOT = '''## Latest page snapshot
uid=1_0 RootWebArea "bohemian rhapsody - YouTube" url="https://www.youtube.com/results?search_query=bohemian"
  uid=1_1 searchbox "Search"
  uid=1_2 button "Search"
  uid=1_3 heading "Filters"
  uid=1_4 StaticText "About 12 million results"
  uid=1_5 link "Sponsored ad that is not a real result" url="https://ads.example/x"
  uid=1_6 link "Queen – Bohemian Rhapsody (Official Video Remastered)" url="https://www.youtube.com/watch?v=fJ9rUzIMcZQ"
  uid=1_7 button "Like"
## Pages
1: My private tab (https://private.example/secret)
2: Another private tab (https://bank.example/account)
'''


def test_snapshot_becomes_numbered_elements_and_text_without_the_tab_list():
    title, url, text, els = parse_snapshot(SNAPSHOT)
    assert title == "bohemian rhapsody - YouTube" and url.startswith("https://www.youtube.com/results")
    assert [(e["n"], e["role"], e["label"]) for e in els][:3] == [(1, "searchbox", "Search"), (2, "button", "Search"), (3, "link", "Sponsored ad that is not a real result")]
    assert els[3]["uid"] == "1_6" and els[3]["href"].endswith("fJ9rUzIMcZQ")
    assert "About 12 million results" in text and "Filters" in text
    assert "private" not in text and "bank.example" not in str(els)
    assert "private" not in strip_page_list(SNAPSHOT)


class FakeClient(DevToolsClient):
    def __init__(self, replies=None):
        self.cmd, self.calls, self.replies = ["x"], [], replies or {}
        self._session = object()
        self.pages = "## Pages\n1: Some tab (https://a.example)\n2: Other (https://b.example)\n"
    def _ids(self):
        import re
        return [int(m.group(1)) for l in self.pages.splitlines() if (m := re.match(r"(\d+):", l))]

    async def call(self, name, args=None, timeout=60.0):
        self.calls.append((name, args or {}))
        if name == "list_pages":
            return Result(self.pages)
        if name == "new_page":
            nid = max(self._ids()) + 1
            self.pages += f"{nid}: New ({args['url']})\n"
            return Result(self.pages)
        r = self.replies.get(name)
        if callable(r):
            r = r(args or {})
        return r or Result("ok")


def browser(**replies):
    c = FakeClient({"take_snapshot": Result(SNAPSHOT), **replies})
    return DevToolsBrowser(c), c


async def test_first_goto_opens_one_tab_then_reuses_it():
    b, c = browser()
    out = await b.goto("youtube.com/results?search_query=bohemian")
    assert b.page_id == 3 and [n for n, _ in c.calls].count("new_page") == 1
    assert "[4] link Queen – Bohemian Rhapsody" in out and "private" not in out and "a.example" not in out
    await b.goto("https://example.com")
    assert [n for n, _ in c.calls].count("new_page") == 1                 # reused: no second tab
    assert ("navigate_page", {"pageId": 3, "type": "url", "url": "https://example.com"}) in c.calls
    assert [n for n, _ in c.calls].count("select_page") == 1              # brought to the front once, so the user can watch


async def test_click_and_type_use_the_real_ids():
    b, c = browser()
    await b.goto("youtube.com")
    await b.click(4)
    assert ("click", {"pageId": 3, "uid": "1_6", "includeSnapshot": False}) in c.calls
    await b.type(1, "daft punk", True)
    assert ("fill", {"pageId": 3, "uid": "1_1", "value": "daft punk", "includeSnapshot": False}) in c.calls
    assert ("press_key", {"pageId": 3, "key": "Enter", "includeSnapshot": False}) in c.calls
    with pytest.raises(LookupError):
        await b.click(99)


async def test_scroll_back_forward_press_media_screenshot():
    b, c = browser(evaluate_script=Result('Script ran on page and returned:\n```json\n"{\\"playing\\":true,\\"at\\":3}"\n```'),
                   take_screenshot=Result("", [(b"\xff\xd8jpegbytes", "image/jpeg")]))
    await b.goto("youtube.com")
    c.replies["evaluate_script"] = Result('```json\n{"moved": true, "percent": 40, "atEnd": false, "atStart": false}\n```')
    assert "40% of the way down" in await b.scroll("down", 1)
    assert any(n == "evaluate_script" and "scrollTo" in a["function"] for n, a in c.calls)
    c.replies["evaluate_script"] = Result('Script ran on page and returned:\n```json\n"{\\"playing\\":true,\\"at\\":3}"\n```')
    await b.back(); assert ("navigate_page", {"pageId": 3, "type": "back"}) in c.calls
    await b.forward(); assert ("navigate_page", {"pageId": 3, "type": "forward"}) in c.calls
    await b.press("PageDown"); assert ("press_key", {"pageId": 3, "key": "PageDown", "includeSnapshot": False}) in c.calls
    assert '"playing":true' in await b.media("play")
    assert await b.screenshot() == (b"\xff\xd8jpegbytes", "image/jpeg")
    with pytest.raises(ValueError):
        await b.scroll("sideways", 1)


async def test_a_scroll_that_does_not_move_the_page_is_reported_not_claimed():
    b, c = browser(evaluate_script=Result('```json\n{"moved": false, "percent": 100, "atEnd": true, "atStart": false}\n```'))
    await b.goto("example.com")
    with pytest.raises(DevToolsError, match="already at the bottom"):
        await b.scroll("down", 1)
    c.replies["evaluate_script"] = Result('```json\n{"moved": false, "percent": 10, "atEnd": false, "atStart": false}\n```')
    with pytest.raises(DevToolsError, match="screen_scroll"):
        await b.scroll("down", 1)                   # the scrolling area is elsewhere: tell the agent which tool to use next


async def test_if_the_user_closed_our_tab_we_start_a_new_one():
    b, c = browser()
    await b.goto("youtube.com")
    c.replies["take_snapshot"] = Result("Error: page 3 not found", is_error=True)
    with pytest.raises(DevToolsError):
        await b.read()
    assert b.page_id is None
    c.replies["take_snapshot"] = Result(SNAPSHOT)
    await b.goto("example.com")
    assert [n for n, _ in c.calls].count("new_page") == 2 and b.page_id == 4


async def test_setup_errors_are_plain_and_never_suggest_workarounds():
    class Off(FakeClient):
        async def call(self, name, args=None, timeout=60.0):
            raise DevToolsError(SETUP_HELP)
    b = DevToolsBrowser(Off())
    with pytest.raises(DevToolsError) as e:
        await b.goto("youtube.com")
    assert "chrome://inspect/#remote-debugging" in str(e.value) and "Do NOT work around" in str(e.value)


async def test_chrome_setup_opens_the_page_and_tells_the_user_what_to_do(monkeypatch):
    import voicectl.agents.devtools as dt
    opened = []
    monkeypatch.setattr(dt.subprocess, "run", lambda cmd, **k: opened.append(cmd))
    with pytest.raises(DevToolsError) as e:
        await DevToolsBrowser(FakeClient()).enable_apple_events()
    assert opened == [["open", "-a", "Google Chrome", "chrome://inspect/#remote-debugging"]]
    assert "switch on Remote Debugging" in str(e.value) and "wait" in str(e.value)


def test_backend_choice(monkeypatch):
    import voicectl.agents.browser as br
    import voicectl.agents.devtools as dt
    from voicectl.core.config import Config
    monkeypatch.setattr(dt, "command", lambda: ["/x/chrome-devtools-mcp"])
    assert type(br.default_browser(Config({}))).__name__ == "DevToolsBrowser"                      # auto prefers DevTools
    monkeypatch.setattr(dt, "command", lambda: None)
    assert type(br.default_browser(Config({}))).__name__ == "ChromeBrowser"                        # no Node: AppleScript
    assert type(br.default_browser(Config({"browser": {"backend": "playwright"}}))).__name__ == "PlaywrightBrowser"


def test_a_reply_that_STARTS_with_the_tab_list_is_stripped_too():
    assert strip_page_list("## Pages\n1: Secret (https://x)\n2: Other (https://y)") == ""
    assert strip_page_list("Done.\n## Pages\n1: Secret (https://x)") == "Done."


async def test_a_failed_element_click_is_retried_on_a_fresh_page_then_handed_to_the_mouse():
    attempts = []
    def click(args):
        attempts.append(args["uid"])
        return Result("Error: The element did not become interactive within the configured timeout.", is_error=True)
    b, c = browser(click=click)
    await b.goto("example.com")
    n = next(n for n, (_, label) in b._labels.items() if label)
    with pytest.raises(DevToolsError, match="screen_click"):
        await b.click(n)
    assert len(attempts) == 2                                                # tried, re-read the page, tried once more
    ok = {"n": 0}
    def flaky(args):
        ok["n"] += 1
        return Result("fail", is_error=True) if ok["n"] == 1 else Result("ok")
    c.replies["click"] = flaky
    await b.read()
    assert "URL:" in await b.click(n)                                         # second attempt on the fresh page works
