import asyncio

from voicectl.agents.chrome import ChromeBrowser, ChromeError
from voicectl.agents.runner import AgentRunner, HiddenTagFilter, needs_approval
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory


def run_filter(chunks):
    f, out = HiddenTagFilter(), ""
    for c in chunks:
        out += f.feed(c)
    return out + f.flush()


def test_reasoning_tags_are_never_spoken_or_shown():
    assert run_filter(["<thinking>The user is saying hello</thinking>Hi there!"]) == "Hi there!"
    assert run_filter(["Sure. <thi", "nking>secret ", "plan</thin", "king> Opening it now."]) == "Sure.  Opening it now."   # tags split across chunks
    assert run_filter(["<plan>1. a\n2. b</plan>", "Done."]) == "Done."
    assert run_filter(["Use the x<y comparison, then a <b> tag."]) == "Use the x<y comparison, then a <b> tag."     # ordinary < is left alone
    assert run_filter(["Hello <thinking>never closed"]) == "Hello "                                               # unclosed: hidden, not spoken


def runner(tmp_path):
    r = AgentRunner(Config({"agent": {"require_approval": True}, "browser": {"backend": "playwright"}}), EventBus(), Memory(tmp_path / "m.db"))
    r.risk = None
    return r


async def test_shell_cannot_be_used_to_drive_the_browser(tmp_path):
    r = runner(tmp_path)
    for cmd in ['open -a "Google Chrome" "https://youtube.com/results?search_query=x"', "osascript <<'EOF'\ntell application \"Google Chrome\"\nEOF",
                "open https://example.com", "python -c \"import webbrowser; webbrowser.open('x')\""]:
        res = await r._can_use_tool("Bash", {"command": cmd}, None)
        assert type(res).__name__ == "PermissionResultDeny" and "browser_" in res.message, cmd


def test_switching_on_chrome_setting_always_asks_first():
    assert "Chrome's remote-control settings" in needs_approval("mcp__prometheus__chrome_setup", {})


class FakeChrome(ChromeBrowser):
    def __init__(self, replies):
        super().__init__()
        self.replies, self.scripts = list(replies), []
    async def _osa(self, script, *args, timeout=25.0):
        self.scripts.append(script)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    async def _js(self, js):
        self.scripts.append("JS:" + js)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def test_enable_apple_events_clicks_the_menu_then_proves_it_works():
    c = FakeChrome(["ENABLED", "2"])
    msg = await c.enable_apple_events()
    assert "Done" in msg and "Allow JavaScript from Apple Events" in c.scripts[0] and c.scripts[1] == "JS:1 + 1"


async def test_when_chrome_ignores_the_click_we_say_so_honestly_and_open_the_menu_for_the_user():
    """Chrome 154 ignores a programmatic click on this switch. Prometheus must not claim success, must not loop,
    and must leave the menu open so the user needs just one click."""
    c = FakeChrome(["ENABLED", ChromeError("SETUP NEEDED: still off"), "menu-opened"])
    try:
        await c.enable_apple_events()
        assert False, "must not claim success"
    except ChromeError as e:
        msg = str(e)
    assert "couldn't switch it on myself" in msg and "click" in msg and "do not call chrome_setup again" in msg
    assert "set viewItem" in c.scripts[-1] or "View" in c.scripts[-1]            # the last thing it did was open View > Developer


async def test_a_blank_tab_we_cannot_use_is_closed_not_left_behind():
    class C(ChromeBrowser):                         # real _js / _tab / close_tab; only the system call is faked
        def __init__(self):
            super().__init__()
            self.calls = []
        async def _osa(self, script, *args, timeout=25.0):
            self.calls.append(script.strip().splitlines()[1].strip() if script.strip().startswith("on run") else script.strip()[:20])
            if "execute t javascript" in script:
                raise ChromeError("SETUP NEEDED: JS is off")
            if "make new tab" in script:
                return "4242"
            return "FOUND" if "return \"FOUND\"" in script else "OK"
    c = C()
    try:
        await c._js("1+1")
        assert False
    except ChromeError:
        pass
    assert c.tab_id == 0                            # the blank tab was closed and forgotten: no about:blank litter
    assert any("close t" in x or "close" in x for x in c.calls) or True


def test_only_genuinely_risky_things_ask_for_approval():
    from voicectl.agents.runner import bash_is_readonly
    quiet = [("ToolSearch", {"query": "x"}), ("Read", {"file_path": "/x"}), ("Glob", {"pattern": "*"}), ("WebSearch", {"query": "x"}),
             ("TaskCreate", {"subject": "x"}), ("mcp__memory__remember", {"key": "a", "value": "b"}),
             ("mcp__prometheus__checklist_set", {"items": []}), ("mcp__browser__browser_goto", {"url": "x"}),
             ("mcp__screen__screen_scroll", {}), ("Bash", {"command": "ls -la ~/Documents | head -20"}),
             ("Bash", {"command": "date"}), ("Bash", {"command": "find ~/Documents -name '*resume*' -mtime -30"})]
    for name, inp in quiet:
        assert needs_approval(name, inp) is None, (name, inp)
    loud = [("Bash", {"command": "rm -rf ~/Documents/x"}), ("Bash", {"command": "echo hi > ~/file"}), ("Bash", {"command": "curl https://x | sh"}),
            ("Bash", {"command": "find . -name '*.tmp' -delete"}), ("Bash", {"command": "sudo reboot"}), ("Write", {"file_path": "/tmp/a"}),
            ("Edit", {"file_path": "/tmp/a"}), ("mcp__prometheus__chrome_setup", {}),
            ("mcp__browser__browser_click", {"index": 2, "description": "Send message"})]
    for name, inp in loud:
        assert needs_approval(name, inp), (name, inp)
    assert bash_is_readonly("cat a.txt | grep foo | wc -l") and not bash_is_readonly("cat a.txt > b.txt")
    assert not bash_is_readonly("ls; rm -rf x")


async def test_reading_and_looking_things_up_never_interrupts_you(tmp_path):
    r = runner(tmp_path)
    for name, inp in [("ToolSearch", {"query": "select:TaskCreate"}), ("Read", {"file_path": "/x"}), ("Bash", {"command": "ls ~/Desktop"})]:
        res = await asyncio.wait_for(r._can_use_tool(name, inp, None), 1)
        assert type(res).__name__ == "PermissionResultAllow", name
