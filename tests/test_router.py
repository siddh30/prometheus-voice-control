import voicectl.actions as pkg
from voicectl.actions.base import actions
from voicectl.core.config import Config
from voicectl.modes.router import Decider, JevDecider, Route, build_deciders, route

actions.discover(pkg)
CFG = Config({})


class Choice:
    def __init__(self, choice, confidence): self.choice, self.confidence = choice, confidence


class FakeResp:
    def __init__(self, route, action=("none", 0.9)): self.choices = {"route": Choice(*route), "action": Choice(*action)}


class FakeJevClient:
    def __init__(self, resp=None, exc=None): self.resp, self.exc, self.calls = resp, exc, 0
    async def system_one(self, state, questions, **kw):
        self.calls += 1
        if self.exc: raise self.exc
        return self.resp


class Fixed(Decider):
    name = "fixed"
    def __init__(self, r): self.r, self.calls = r, 0
    async def decide(self, text, recent, job=None): self.calls += 1; return self.r


async def test_fast_path_skips_models():
    d = Fixed(Route("chat"))
    r = await route("open safari", [], CFG, [d])
    assert r.kind == "action" and r.how == "pattern" and d.calls == 0


async def test_jev_chat_and_agent():
    for kind in ("chat", "agent"):
        jev = JevDecider(client=FakeJevClient(FakeResp((kind, 0.93))))
        r = await route("find product manager jobs that fit my resume and rank them", [], CFG, [jev])
        assert (r.kind, r.how) == (kind, "jev") and r.confidence == 0.93


async def test_jev_action_args_from_regex_else_extraction():
    jev = JevDecider(client=FakeJevClient(FakeResp(("action", 0.9), ("open_app", 0.9))))
    jev._extract_args = lambda cls, text: {"app": "FROM_EXTRACTION"}
    # phrasing the regex can't fully match -> args come from the extraction call
    r = await route("could you possibly fire up the notes application for me", [], CFG, [jev])
    assert r.action.name == "open_app" and r.args == {"app": "FROM_EXTRACTION"} and r.how == "jev"
    # a no-argument action never needs extraction
    jev2 = JevDecider(client=FakeJevClient(FakeResp(("action", 0.9), ("sleep_mac", 0.9))))
    jev2._extract_args = lambda cls, text: (_ for _ in ()).throw(AssertionError("should not extract"))
    r = await route("I am heading out so go ahead and put the machine to sleep now", [], CFG, [jev2])
    assert r.action.name == "sleep_mac" and r.args == {}


async def test_low_confidence_falls_back_to_llm():
    jev = JevDecider(client=FakeJevClient(FakeResp(("chat", 0.40))))
    llm = Fixed(Route("agent", how="llm"))
    r = await route("hmm what about that thing from before maybe", [], CFG, [jev, llm])
    assert r.how == "llm" and r.kind == "agent" and llm.calls == 1


async def test_jev_error_falls_back_and_never_raises():
    jev = JevDecider(client=FakeJevClient(exc=RuntimeError("401")))
    llm = Fixed(Route("chat", how="llm"))
    assert (await route("tell me a joke about routers please", [], CFG, [jev, llm])).how == "llm"
    assert (await route("tell me a joke about routers please", [], CFG, [jev])).how == "default"


def test_auto_uses_jev_only_with_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert [d.name for d in build_deciders(Config({}))] == ["llm"]
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert [d.name for d in build_deciders(Config({}))] == ["jev", "llm"]
    assert [d.name for d in build_deciders(Config({"assistant": {"router": "llm"}}))] == ["llm"]


def test_polite_phrasing_is_normalized_for_the_instant_path():
    from voicectl.modes.router import normalize
    assert normalize("Can you open LinkedIn for me, please?") == "open LinkedIn"
    assert normalize("Hey, could you please put the computer to sleep") == "put the computer to sleep"
    assert normalize("I'd like you to open the downloads folder now") == "open the downloads folder"
    assert normalize("tell me a joke") == "tell me a joke"


async def test_polite_request_skips_the_model():
    d = Fixed(Route("chat"))
    r = await route("Can you open LinkedIn for me, please?", [], CFG, [d])
    assert (r.kind, r.how, r.action.name, r.args) == ("action", "pattern", "open_app", {"app": "LinkedIn"}) and d.calls == 0


def test_open_linkedin_opens_the_website_not_an_app(monkeypatch):
    import voicectl.actions._sys as s
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    res = actions.get("open_app")().run(app="LinkedIn")
    assert calls == [["open", "https://www.linkedin.com"]] and res.spoken == "Opening LinkedIn."
