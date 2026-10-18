import asyncio

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes import assistant as assistant_mod
from voicectl.modes.assistant import AssistantMode
from voicectl.modes.base import ModeContext
from voicectl.modes.router import Route, fast_route, parse_yes_no


class FakeRunner:
    def __init__(self):
        self.mode_name = "agent"
        self.pending_approval = False
        self.started, self.finish, self.interrupted, self.contexts, self.approvals = asyncio.Event(), None, 0, [], []
        self.result = "Found three roles."
        self.task = ""

    def status(self): return f"Working on: {self.task}." if self.task else "Idle."
    async def warm_up(self): ...
    async def interrupt(self): self.interrupted += 1
    def respond_approval(self, approved): self.approvals.append({"command": "approval", "approved": approved})

    async def run_turn(self, text, context=""):
        self.task = text
        self.contexts.append(context)
        self.started.set()
        try:
            if self.finish is not None:
                await self.finish.wait()
            return self.result
        finally:
            self.task = ""


class FakeSpeaker:
    active = False
    first_audio_at = 0.0
    def __init__(self): self.said = []
    async def speak(self, text): self.said.append(text)
    async def speak_many(self, chunks, clean=True, show=False):
        async for c in chunks:
            self.said.append(c)
    def stop(self): ...
    def warm_output(self): ...
    def prefetch(self, t): ...


def make(monkeypatch, routes):
    cfg = Config({"tts": {"enabled": True}, "assistant": {"speculative_chat": False}})
    bus = EventBus()
    ctx = ModeContext(cfg, bus, FakeSpeaker())
    runner = FakeRunner()
    ctx.shared["agent_runner"] = runner
    m = AssistantMode(ctx)
    m.history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    seq = list(routes)

    async def fake_route(text, recent, config, deciders, job=None):
        return seq.pop(0)
    monkeypatch.setattr(assistant_mod, "route", fake_route)
    monkeypatch.setattr(assistant_mod, "fast_route", lambda text, job=None: fast_route(text, job) if text.lower().strip() in ("stop", "cancel that", "never mind") else None)

    async def tokens():
        for t in ["Pretty good, ", "thanks for asking."]:
            yield t
    m._tokens = tokens
    return m, runner, ctx.speaker, bus


async def settle(t=0.05): await asyncio.sleep(t)


async def test_a_task_becomes_a_background_job_and_the_conversation_stays_free(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent", how="llm")])
    runner.finish = asyncio.Event()
    m.history.append({"role": "user", "content": "find PM jobs on LinkedIn"})
    reply = await asyncio.wait_for(m.respond("find PM jobs on LinkedIn"), 2)    # returns after the acknowledgement
    assert reply in ("On it.", "Sure, on it.", "Got it, working on that.", "Okay, let me do that.")
    assert spk.said == [reply]
    assert m._job_running() and runner.mode_name == "assistant"
    runner.finish.set(); await settle(0.1)
    assert not m._job_running() and "Found three roles." in " ".join(m.notes)


async def test_the_result_is_announced_once_it_is_finished_and_nobody_is_talking(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent")])
    runner.finish = asyncio.Event()
    await m.respond("do the thing")
    m._user_talking = True                       # the user is mid-sentence
    runner.finish.set(); await settle(0.3)
    announced = [t for t in spk.said if "three roles" in t]
    assert not announced                         # held back: must not talk over the user
    m._user_talking = False; await settle(0.5)
    assert any("three roles" in t for t in spk.said)


async def test_chatting_while_the_agent_works_does_not_cancel_it(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent"), Route("chat")])
    runner.finish = asyncio.Event()
    await m.respond("search linkedin")
    await runner.started.wait()
    reply = await m.respond("how are you doing?")                # a chat turn in the meantime
    assert "Pretty good" in reply
    assert m._job_running() and runner.interrupted == 0          # the job was untouched
    runner.finish.set(); await settle(0.1)


async def test_stop_cancels_the_running_job(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent")])
    runner.finish = asyncio.Event()
    await m.respond("search linkedin"); await runner.started.wait()
    reply = await m.respond("stop")
    assert reply == "Stopped." and not m._job_running() and runner.interrupted == 1
    assert "Okay, stopped." in spk.said


async def test_stop_with_nothing_running_is_harmless(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("control")])
    assert await m.respond("never mind") == "Nothing is running."


async def test_a_new_task_replaces_the_running_one(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent"), Route("agent")])
    runner.finish = asyncio.Event()
    await m.respond("task one"); await runner.started.wait()
    first = m.job
    await m.respond("actually, task two")
    assert first.cancelled() and runner.interrupted == 1 and m.job_text == "actually, task two"
    runner.finish.set(); await settle(0.1)


async def test_spoken_yes_and_no_answer_an_approval(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [])
    runner.pending_approval = True
    assert await m.respond("yes, go ahead") == "Okay."
    assert await m.respond("no") == "Okay, I won't."
    assert [a["approved"] for a in runner.approvals] == [True, False]
    runner.pending_approval = False
    assert parse_yes_no("tell me about the roles") is None        # ordinary talk is not an answer


async def test_the_agent_receives_the_recent_conversation(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent")])
    m.history += [{"role": "user", "content": "the second one looks good"}, {"role": "assistant", "content": "Great choice."},
                  {"role": "user", "content": "open that one for me"}]       # the current request is last, as in a real turn
    await m.respond("open that one for me"); await runner.started.wait()
    ctx = runner.contexts[0]
    assert "second one looks good" in ctx and "hello" in ctx


async def test_chat_that_recognises_a_task_hands_it_to_the_agent_instead_of_refusing(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("chat")])
    runner.finish = asyncio.Event()

    async def tokens():
        yield "<delegate/>"
    m._tokens = tokens
    reply = await m.respond("could you scroll that page for me")
    assert m._job_running() and reply in ("On it.", "Sure, on it.", "Got it, working on that.", "Okay, let me do that.")
    assert not any("can't" in t.lower() for t in spk.said)
    runner.finish.set(); await settle(0.1)


async def test_pressing_the_key_does_not_stop_a_running_job(monkeypatch):
    """Ctrl+F10 is for talking: you can give instructions while it works, and say "stop" to cancel (the spoken stop is tested above)."""
    m, runner, spk, bus = make(monkeypatch, [Route("agent")])
    runner.finish = asyncio.Event()
    await m.respond("search linkedin"); await runner.started.wait()
    m.press()
    await settle(0.1)
    assert m._job_running() and runner.interrupted == 0


async def test_chat_knows_what_the_agent_is_doing(monkeypatch):
    m, runner, spk, bus = make(monkeypatch, [Route("agent")])
    runner.finish = asyncio.Event()
    await m.respond("search linkedin"); await runner.started.wait()
    assert "Working on: search linkedin" in m.agent_status()
    runner.finish.set(); await settle(0.1)
    assert "Found three roles" in m.agent_status()                # and what it just finished


def test_fast_paths_for_followups_and_control():
    assert fast_route("scroll down").kind == "agent"
    assert fast_route("go back").kind == "agent"
    assert fast_route("click the second one").kind == "agent"
    assert fast_route("open the first one").kind == "agent"
    assert fast_route("what's on the screen").kind == "agent"
    assert fast_route("stop", job="searching").kind == "control"
    assert fast_route("stop") is None                             # nothing running: not a control command
    assert fast_route("open Safari").kind == "action"
    assert fast_route("open the downloads folder").action.name == "open_folder"
    assert fast_route("open a browser").action.name == "open_app"


def test_yes_no_variants():
    for t in ["yes", "Yes, go ahead.", "yeah do it", "sure", "okay", "go ahead", "approved", "yes please"]:
        assert parse_yes_no(t) is True, t
    for t in ["no", "No, don't.", "nope", "don't do that", "not now", "cancel"]:
        assert parse_yes_no(t) is False, t
    for t in ["yes I think the second role is better", "tell me more", "no wait, what about Google"]:
        assert parse_yes_no(t) is None, t
