import voicectl.actions as pkg
from voicectl.actions import router
from voicectl.actions.base import actions

actions.discover(pkg)


def m(text):
    r = router.match_pattern(text)
    return (r[0].name, r[1]) if r else None


def test_patterns():
    assert m("Open Safari.") == ("open_app", {"app": "Safari"})
    assert m("open the downloads folder") == ("open_folder", {"folder": "downloads"})
    assert m("Open my documents") == ("open_folder", {"folder": "documents"})
    assert m("go to github.com") == ("open_url", {"url": "github.com"})
    assert m("search for best pizza near me") == ("search_web", {"query": "best pizza near me"})
    assert m("set volume to 30 percent") == ("set_volume", {"level": "30"})
    assert m("put the computer to sleep")[0] == "sleep_mac"
    assert m("lock screen")[0] == "lock_screen"
    assert m("mute")[0] == "mute"
    assert m("what is the airspeed of a swallow") is None


def test_run_action_uses_subprocess(monkeypatch):
    calls = []
    import voicectl.actions._sys as s
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    res = router.run_action("open safari", use_llm=False)
    assert res.ok and calls == [["open", "-a", "Safari"]]
    assert router.run_action("sleep", use_llm=False).ok and calls[-1] == ["pmset", "sleepnow"]


def test_unknown_without_llm():
    assert not router.run_action("flibbertigibbet", use_llm=False).ok


def test_open_only_claims_real_apps_and_known_sites():
    assert m("open Safari") == ("open_app", {"app": "Safari"})
    assert m("open LinkedIn") == ("open_app", {"app": "LinkedIn"})
    assert m("open the browser") == ("open_app", {"app": "browser"})
    # these are requests for the agent, not launcher commands:
    assert m("open Obvious by Chris Brown on YouTube") is None
    assert m("open obvious spectres brown on youtube") is None
    assert m("open my resume and look for jobs") is None


def test_plain_google_search_only_when_no_site_is_named():
    assert m("search for best pizza near me") == ("search_web", {"query": "best pizza near me"})
    assert m("search YouTube for obvious by chris brown") is None
    assert m("search for product manager jobs on LinkedIn") is None
