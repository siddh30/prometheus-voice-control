import asyncio

import pytest

from voicectl.actions.base import ActionResult, actions
from voicectl.actions.system import parse_number
from voicectl.agents.risk import JevRisk
from voicectl.agents.runner import AgentRunner
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory
from voicectl.modes.base import ModeContext
from voicectl.modes.prometheus import PrometheusMode
from voicectl.modes.router import JevDecider, fast_route
import voicectl.actions as pkg

actions.discover(pkg)


class Noul:
    def __init__(self, v): self.noul = v


class Resp:
    def __init__(self, noul=None, choices=None):
        self.nouls = {"risky": Noul(noul)} if noul is not None else {}
        self.choices = choices or {}


class FakeJev:
    def __init__(self, resp=None, exc=None, delay=0.0): self.resp, self.exc, self.delay, self.calls = resp, exc, delay, []
    async def system_one(self, state, questions, **kw):
        self.calls.append(state)
        await asyncio.sleep(self.delay)
        if self.exc: raise self.exc
        return self.resp


async def test_risk_score_and_threshold():
    r = JevRisk(0.4, client=FakeJev(Resp(0.73)))
    assert await r.score("Clicking: Send message", "LinkedIn") == 0.73 and await r.is_risky("x")
    assert not await JevRisk(0.4, client=FakeJev(Resp(0.05))).is_risky("Clicking: Play")


async def test_risk_never_blocks_or_raises():
    assert await JevRisk(client=FakeJev(exc=RuntimeError("401"))).score("x") is None
    assert await JevRisk(timeout_s=0.05, client=FakeJev(Resp(0.9), delay=1)).score("x") is None      # too slow: ignore it
    assert not await JevRisk(timeout_s=0.05, client=FakeJev(Resp(0.9), delay=1)).is_risky("x")


def runner_with(risk, tmp_path):
    cfg = Config({"agent": {"require_approval": True}, "browser": {"backend": "playwright"}})
    r = AgentRunner(cfg, EventBus(), Memory(tmp_path / "m.db"))
    r.risk = risk
    r.browser.last_title, r.browser.last_url = "Checkout", "https://shop.example/checkout"
    return r


async def test_gate_asks_when_jev_flags_what_the_word_list_missed(tmp_path):
    r = runner_with(JevRisk(0.4, client=FakeJev(Resp(0.8))), tmp_path)
    asked = []

    async def watch():
        async for ev in r.bus.subscribe():
            if ev.type == "approval":
                asked.append(ev.data["text"]); r.respond_approval(False)
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    res = await r._can_use_tool("mcp__browser__browser_click", {"index": 3, "description": "Place bet"}, None)   # no risky WORD in it
    w.cancel()
    assert asked == ["Clicking: Place bet?"] and type(res).__name__ == "PermissionResultDeny"


async def test_gate_does_not_nag_for_safe_clicks_and_skips_jev_for_reads(tmp_path):
    jev = FakeJev(Resp(0.04))
    r = runner_with(JevRisk(0.4, client=jev), tmp_path)
    res = await r._can_use_tool("mcp__browser__browser_click", {"index": 3, "description": "Open the first video"}, None)
    assert type(res).__name__ == "PermissionResultAllow" and len(jev.calls) == 1 and "Checkout" in jev.calls[0]["page"]
    await r._can_use_tool("mcp__browser__browser_type", {"index": 1, "text": "lofi", "submit": False, "description": "search"}, None)
    assert len(jev.calls) == 1                       # typing without Enter is never risky: not even asked


async def test_the_word_list_still_works_without_jev(tmp_path):
    r = runner_with(None, tmp_path)
    asked = []

    async def watch():
        async for ev in r.bus.subscribe():
            if ev.type == "approval":
                asked.append(ev.data["text"]); r.respond_approval(True)
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    res = await r._can_use_tool("mcp__browser__browser_click", {"index": 3, "description": "Easy Apply button"}, None)
    w.cancel()
    assert asked and type(res).__name__ == "PermissionResultAllow"


def test_volume_number_words():
    assert [parse_number(x) for x in ["30", "thirty", "forty-two", "twenty five", "a hundred", "zero", "loud"]] == [30, 30, 42, 25, 100, 0, None]
    r = fast_route("set the volume to thirty five percent")
    assert r.action.name == "set_volume" and r.args == {"level": "thirty five"}
    assert fast_route("turn the volume up") is None                  # no level given: the agent decides what that means


def test_set_volume_runs_with_a_spoken_number(monkeypatch):
    import voicectl.actions._sys as s
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    res = actions.get("set_volume")().run(level="forty five")
    assert res.ok and "45" in calls[0][-1]
    assert not actions.get("set_volume")().run(level="loud").ok


async def test_jev_declines_to_guess_arguments_when_extraction_is_off():
    from voicectl.modes.router import JevDecider
    class Ch:
        def __init__(self, choice, confidence): self.choice, self.confidence = choice, confidence
    resp = Resp(choices={"route": Ch("action", 0.97), "action": Ch("open_app", 0.95)})
    jev = JevDecider(min_confidence=0.85, allow_extraction=False, client=FakeJev(resp))
    jev._extract_args = lambda cls, text: pytest.fail("must not guess arguments")
    assert await jev.decide("could you possibly fire up the notes thing for me", [], None) is None


async def test_prometheus_runs_clearly_instant_commands_through_jev_and_sends_the_rest_to_the_agent(monkeypatch):
    class Ch:
        def __init__(self, choice, confidence): self.choice, self.confidence = choice, confidence
    cfg = Config({"tts": {"enabled": False}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}})
    ctx = ModeContext(cfg, EventBus(), None)

    class Runner:
        mode_name = "x"; pending_approval = False; turns = []
        async def stream_turn(self, text, context="", hints="", complexity="", publish_result=True):
            from voicectl.agents.runner import Finished
            Runner.turns.append(text); yield Finished("agent handled it")
        async def warm_up(self): ...
        async def interrupt(self): ...
        def status(self): return ""
    ctx.shared["agent_runner"] = Runner()
    m = PrometheusMode(ctx)
    ran = []
    monkeypatch.setattr(actions.get("sleep_mac"), "run", lambda self: ran.append("sleep") or ActionResult(True, "Sleeping", "Sleeping."))

    m.jev = JevDecider(min_confidence=0.85, allow_extraction=False, client=FakeJev(Resp(choices={"route": Ch("action", 0.97), "action": Ch("sleep_mac", 0.96)})))
    assert await m.respond("go ahead and put the machine to sleep") == "Sleeping"
    assert ran == ["sleep"] and Runner.turns == []

    m.jev = JevDecider(min_confidence=0.85, allow_extraction=False, client=FakeJev(Resp(choices={"route": Ch("action", 0.60), "action": Ch("sleep_mac", 0.9)})))
    assert await m.respond("hmm maybe sleep or not") == "agent handled it"      # unsure: never guess, ask the agent
    m.jev = JevDecider(min_confidence=0.85, allow_extraction=False, client=FakeJev(exc=RuntimeError("down")))
    assert await m.respond("do something with my files please") == "agent handled it"   # Jev down: agent as always
    assert ran == ["sleep"]
