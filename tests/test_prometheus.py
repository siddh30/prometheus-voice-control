import asyncio

from voicectl.agents.runner import Finished, TextDelta, TextEnd, TextStart
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.prometheus import PrometheusMode


class FakeRunner:
    """Scripted agent: stream_turn yields whatever the test queues, awaiting any asyncio.Event it finds in the script."""
    def __init__(self):
        self.mode_name = "agent"
        self.pending_approval = False
        self.script = []                    # list of events or asyncio.Event/float (wait) items
        self.interrupted, self.contexts, self.approvals, self.turns = 0, [], [], []

    def status(self): return "Idle."
    async def warm_up(self): ...
    async def interrupt(self): self.interrupted += 1
    def respond_approval(self, approved): self.approvals.append({"command": "approval", "approved": approved})

    async def stream_turn(self, text, context="", hints="", complexity="", publish_result=True):
        self.turns.append(text); self.contexts.append(context); self.hints = getattr(self, 'hints', []) + [hints]
        for item in self.script:
            if isinstance(item, asyncio.Event):
                await item.wait()
            elif isinstance(item, (int, float)):
                await asyncio.sleep(item)
            else:
                yield item


class FakeSpeaker:
    active = False
    first_audio_at = 0.0

    def __init__(self): self.said, self.stopped = [], 0
    async def speak(self, text): self.said.append(text)
    async def speak_many(self, chunks, clean=True, show=False):
        async for c in chunks:
            self.said.append(c)
            await asyncio.sleep(0)
    def stop(self): self.stopped += 1
    def warm_output(self): ...
    def prefetch(self, t): ...


def make():
    cfg = Config({"tts": {"enabled": True}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}, "agent": {"fast_chat": False}})
    bus = EventBus()
    ctx = ModeContext(cfg, bus, FakeSpeaker())
    runner = FakeRunner()
    ctx.shared["agent_runner"] = runner
    return PrometheusMode(ctx), runner, ctx.speaker, bus


def say(bid, text):
    return [TextStart(bid), TextDelta(bid, text), TextEnd(bid)]


async def test_narration_is_spoken_in_order_while_the_task_runs():
    m, runner, spk, bus = make()
    gate = asyncio.Event()
    runner.script = [*say(1, "Opening YouTube and searching for it. "), gate, *say(2, "Found it. Playing the first result. "),
                     Finished("Playing it now.")]
    task = asyncio.create_task(m.respond("play lofi on youtube"))
    await asyncio.sleep(0.1)
    assert spk.said == ["Opening YouTube and searching for it."]      # spoken NOW, before the task has finished
    assert not task.done()
    gate.set()
    assert await asyncio.wait_for(task, 2) == "Playing it now."
    assert spk.said == ["Opening YouTube and searching for it.", "Found it.", "Playing the first result."]


async def test_the_user_talking_over_it_interrupts_the_agent_and_stops_speech():
    m, runner, spk, bus = make()
    runner.script = [*say(1, "Let me look into that for you. "), asyncio.Event()]     # then stuck "working"
    task = asyncio.create_task(m.respond("research something"))
    await asyncio.sleep(0.1)
    task.cancel()                                  # what _cut_off() does when you start talking
    await asyncio.gather(task, return_exceptions=True)
    assert runner.interrupted == 1 and spk.stopped >= 1


async def test_a_spoken_yes_answers_the_approval_without_cancelling_the_waiting_step():
    m, runner, spk, bus = make()
    runner.script = [asyncio.Event()]
    turn = asyncio.create_task(m.respond("apply to the first job"))
    await asyncio.sleep(0.05)
    runner.pending_approval = True                 # the agent is paused on "Apply?"
    m._turn = turn
    m._on_record_start()                           # you begin to say "yes"
    await asyncio.sleep(0.05)
    assert not turn.cancelled() and not turn.done()                       # the waiting step survived
    assert await m.intercept("Yes, go ahead.") is True
    assert runner.approvals == [{"command": "approval", "approved": True}]
    await asyncio.sleep(0.05)
    assert "Okay." in spk.said
    turn.cancel(); await asyncio.gather(turn, return_exceptions=True)


async def test_no_denies_and_ordinary_talk_is_not_an_answer():
    m, runner, spk, bus = make()
    runner.pending_approval = True
    assert await m.intercept("no, don't") is True and runner.approvals[-1]["approved"] is False
    assert await m.intercept("what about the second one") is False


async def test_stop_while_working_cancels_and_confirms():
    m, runner, spk, bus = make()
    runner.script = [asyncio.Event()]
    m._turn = asyncio.create_task(m._run_turn(None, "dig into this", None))
    await asyncio.sleep(0.05)
    assert m._busy()
    assert await m.intercept("stop") is True
    await asyncio.sleep(0.2)
    assert runner.interrupted >= 1 and "Okay, stopped." in spk.said


async def test_stop_when_idle_is_just_a_normal_sentence():
    m, runner, spk, bus = make()
    assert await m.intercept("stop") is False


async def test_exact_commands_skip_the_model_and_are_remembered_for_the_agent(monkeypatch):
    import voicectl.actions._sys as s
    calls = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: calls.append(cmd) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    m, runner, spk, bus = make()
    assert await m.respond("open Safari") == "Opened Safari"
    assert calls == [["open", "-a", "Safari"]] and runner.turns == []         # no model call
    assert spk.said == ["Opening Safari."]
    runner.script = [Finished("done")]
    await m.respond("now search for cats there")
    assert "Opened Safari" in runner.contexts[0]                              # the agent knows what just happened


async def test_everything_else_goes_to_the_one_agent_even_small_talk_and_tasks():
    m, runner, spk, bus = make()
    runner.script = [*say(1, "Pretty good, thanks. "), Finished("Pretty good, thanks.")]
    assert await m.respond("how are you doing") == "Pretty good, thanks."
    runner.script = [Finished("Scrolled.")]
    await m.respond("scroll down and click the red button")
    assert runner.turns == ["how are you doing", "scroll down and click the red button"]   # one brain, no router, no refusal path


async def test_no_voice_just_shows_the_words():
    cfg = Config({"tts": {"enabled": False}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}, "agent": {"fast_chat": False}})
    bus = EventBus()
    ctx = ModeContext(cfg, bus, None)
    runner = FakeRunner(); ctx.shared["agent_runner"] = runner
    m = PrometheusMode(ctx)
    runner.script = [*say(1, "Hello there."), Finished("Hello there.")]
    shown = []

    async def watch():
        async for ev in bus.subscribe():
            if ev.type == "result": shown.append(ev.data["text"])
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    await m.respond("hi"); await asyncio.sleep(0.05); w.cancel()
    assert shown[-1] == "Hello there."


async def test_a_status_is_never_spoken_twice_in_one_turn():
    from voicectl.modes.prometheus import _Voice
    from voicectl.modes.base import ModeContext
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    v = _Voice(ModeContext(Config({"tts": {"enabled": False}}), EventBus()))
    assert v.is_repeat("Playing it now.") is False
    assert v.is_repeat("Playing it now.") is True            # said again after the tool finished
    assert v.is_repeat("Playing it now!") is True
    assert v.is_repeat("Found it, it's the official video.") is False
    assert v.is_repeat("Playing it now.") is True


async def test_a_silent_agent_is_dropped_for_a_clean_session_and_the_request_retried(tmp_path, monkeypatch):
    """The thread that just hangs ("thinking" forever) must not hang the user: after the watchdog it restarts clean and retries once."""
    from voicectl.agents import runner as rm
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory

    class Silent:
        async def query(self, p): pass
        async def interrupt(self): pass
        def receive_response(self):
            async def gen():
                await asyncio.sleep(60)
                yield None
            return gen()
    class Quick:
        async def query(self, p): self.p = p
        async def interrupt(self): pass
        def receive_response(self):
            async def gen():
                return
                yield
            return gen()

    r = rm.AgentRunner(Config({"agent": {"first_response_timeout_s": 0.2}, "browser": {"backend": "playwright"}}), EventBus(), Memory(tmp_path / "m.db"))
    clients = [Silent(), Quick()]
    async def ensure():
        return clients[0] if r._tries == 0 else clients[1], 1
    r._tries = 0
    async def fake_ensure():
        c = clients[min(r._tries, 1)]
        return c, 1
    r._ensure_session = fake_ensure
    ended = []
    async def end(): ended.append(1); r._tries += 1
    r.end_session = end
    out = [ev async for ev in r.stream_turn("hello")]
    assert ended == [1] and isinstance(out[-1], rm.Finished)


async def test_a_restart_keeps_the_context_as_a_compact_recap_and_a_new_conversation_drops_it(tmp_path):
    from voicectl.agents import runner as rm
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.maintenance import Maintenance
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    old = mem.new_session(); mem.record_turn(old, "claude-sid-1")
    mem.log_turn(old, "find ML director roles at Apple", "I found three on jobs.apple.com")
    r = rm.AgentRunner(Config({"browser": {"backend": "playwright"}}), EventBus(), mem)
    class C:
        async def disconnect(self): pass
    r._client = C()
    await r.end_session()                                                     # what a restart / idle timeout does
    prompt = r._system_prompt()
    assert "find ML director roles at Apple" in prompt and "jobs.apple.com" in prompt and "EarlierConversation" in prompt
    assert len(prompt) < len(rm.ROOT.joinpath("prompts", r.prompt_name).read_text()) + 2500        # compact, not the whole thread

    m = Maintenance(Config({}), EventBus(), mem, lambda: True, lambda: r, data_dir=tmp_path)
    r.interrupt = lambda: asyncio.sleep(0)
    await m.new_conversation()                                                # the user asked for a clean slate
    assert "EarlierConversation" not in r._system_prompt()


async def test_a_long_conversation_is_compacted_into_a_summary_plus_a_short_recap(tmp_path, monkeypatch):
    from voicectl.agents import runner as rm
    from voicectl.core import maintenance
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    sid = mem.new_session()
    for i in range(30):
        mem.record_turn(sid, "cs"); mem.log_turn(sid, f"question {i}", f"answer {i}")
    async def fake_summary(text): return "Looked at ML roles; leaning Apple and Bloomberg."
    monkeypatch.setattr(maintenance, "summarize", fake_summary)
    r = rm.AgentRunner(Config({"agent": {"compact_after_turns": 20, "recap_turns": 5}, "browser": {"backend": "playwright"}}), EventBus(), mem)
    class C:
        async def disconnect(self): pass
    r._client = C()
    row = mem.active_session(1e12)
    await r._compact(row)
    assert mem.facts()["session_digest"].startswith("Looked at ML roles")
    prompt = r._system_prompt()
    assert "session_digest" in prompt and "question 29" in prompt and "question 24" not in prompt   # summary + only the last 5 verbatim


async def test_a_single_step_request_that_keeps_calling_tools_is_stopped_and_the_plan_is_cleared(tmp_path):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    from voicectl.agents import runner as rm
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory

    class Greedy:
        interrupted = False
        async def query(self, p): pass
        async def interrupt(self): self.interrupted = True
        def receive_response(self):
            async def gen():
                for i in range(20):
                    yield AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name="mcp__browser__browser_read", input={})], model="m")
            return gen()
    bus = EventBus()
    seen = []
    async def watch():
        async for ev in bus.subscribe():
            seen.append(ev)
    task = asyncio.create_task(watch()); await asyncio.sleep(0)
    r = rm.AgentRunner(Config({"agent": {"single_step_tool_cap": 3}, "browser": {"backend": "playwright"}}), bus, Memory(tmp_path / "m.db"))
    c = Greedy()
    async def ensure(): return c, r.memory.new_session()
    r._ensure_session = ensure
    r.checklist = [{"text": "x", "state": "pending"}]
    async def interrupt(): c.interrupted = True
    r.interrupt = interrupt
    out = [ev async for ev in r.stream_turn("open it in another tab", complexity="single")]
    await asyncio.sleep(0.05); task.cancel()
    steps = [e for e in seen if e.type == "step" and not e.data.get("done")]
    assert len(steps) == 3 and c.interrupted and isinstance(out[-1], rm.Finished)       # stopped after 3 tool calls, not 20
    assert any(e.type == "notice" and "one-step" in e.data["text"] for e in seen)
    assert [e for e in seen if e.type == "checklist"][-1].data["items"] == []            # the plan is gone when the work is


async def test_the_recap_survives_restarts_that_open_empty_sessions(tmp_path):
    """Regression: each launch opens an empty session; the next launch must still recap the last conversation that had turns."""
    from voicectl.agents import runner as rm
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    real = mem.new_session(); mem.record_turn(real, "cs"); mem.log_turn(real, "compare Apple and Bloomberg roles", "Apple's services team fits best")
    mem.new_session(); mem.new_session()                                    # two launches that never got a turn
    r = rm.AgentRunner(Config({"browser": {"backend": "playwright"}}), EventBus(), mem)
    assert "compare Apple and Bloomberg roles" in r._system_prompt()


def _quick_chat_mode(tmp_path, pieces, spoken):
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory
    from voicectl.modes.base import ModeContext
    from voicectl.modes.prometheus import PrometheusMode
    from voicectl.agents import runner as rm

    class Spk:
        active = False
        first_audio_at = 0.0
        async def speak_many(self, chunks, clean=True, show=False):
            async for c in chunks: spoken.append(c)
        def stop(self): pass
        def warm_output(self): pass
    ctx = ModeContext(Config({"agent": {"fast_chat": True}, "tts": {"enabled": True}, "browser": {"backend": "playwright"}}), EventBus())
    ctx.speaker = Spk()
    m = PrometheusMode(ctx)
    runner = rm.AgentRunner(ctx.config, ctx.bus, Memory(tmp_path / "m.db"))
    ctx.shared["agent_runner"] = runner
    m._runner = lambda: runner
    class Client:
        @staticmethod
        async def stream(**kw):
            Client.kw = kw
            if isinstance(pieces, Exception): raise pieces
            for p in pieces: yield p
    from voicectl import llm
    llm.set_llm(Client)
    m.history = [{"role": "user", "content": "how are you"}]
    return m, runner, Client


async def test_plain_chat_is_answered_quickly_by_the_small_model_without_the_agent(tmp_path):
    spoken = []
    m, runner, client = _quick_chat_mode(tmp_path, ["I'm doing great. ", "How can I help?"], spoken)
    out = await m._fast_chat("how are you")
    assert out == "I'm doing great. How can I help?" and spoken and client.kw["model"] == "claude-haiku-4-5"
    assert any("Quick chat" in n for n in m._side_notes)                       # the agent is told what was said, so context continues
    assert runner.memory.recent_turns(1) == [("how are you", "I'm doing great. How can I help?")]


async def test_quick_chat_hands_over_to_the_agent_without_saying_anything_when_tools_are_needed(tmp_path):
    for pieces in (["NEEDS_AGENT"], ["NEEDS", "_AGENT"], ["needs_agent"]):
        spoken = []
        m, *_ = _quick_chat_mode(tmp_path, pieces, spoken)
        assert await m._fast_chat("what is on my calendar") is None and spoken == []         # nothing was spoken: the agent answers cleanly
    spoken = []
    m, *_ = _quick_chat_mode(tmp_path, ["Never spoken"], spoken)
    for text in ["open linkedin", "scroll down on this page", "can you play a song", "check my screen", "send an email to bob"]:
        assert await m._fast_chat(text) is None, text                                           # obvious tool requests never reach the small model
    assert spoken == []


async def test_quick_chat_failure_falls_back_to_the_agent(tmp_path):
    spoken = []
    m, *_ = _quick_chat_mode(tmp_path, RuntimeError("api down"), spoken)
    assert await m._fast_chat("how are you") is None and spoken == []
    m.ctx.config.raw["agent"]["fast_chat"] = False
    m2, *_ = _quick_chat_mode(tmp_path, ["hi"], [])
    m2.ctx.config.raw["agent"]["fast_chat"] = False
    assert await m2._fast_chat("how are you") is None                                           # the switch turns it off
