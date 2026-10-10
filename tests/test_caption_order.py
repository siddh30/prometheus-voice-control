"""The caption must never show the whole answer before the voice has read it (the text used to flash in full, then restart word by word)."""
import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

from voicectl.agents.runner import AgentRunner, Finished, TextDelta
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory


def ev(e):
    return StreamEvent(uuid="u", session_id="s", event=e)


def script(answer_words):
    msgs = [ev({"type": "message_start"}), ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
    for w in answer_words:
        msgs.append(ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}}))
    msgs += [ev({"type": "content_block_stop", "index": 0}),
             AssistantMessage(content=[TextBlock("".join(answer_words))], model="m"),
             ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                           result="".join(answer_words))]
    return msgs


class FakeClient:
    def __init__(self, msgs): self.msgs = msgs
    async def query(self, prompt): ...
    async def receive_response(self):
        for m in self.msgs:
            yield m


def runner(tmp_path, words):
    r = AgentRunner(Config({"browser": {"backend": "playwright"}}), EventBus(), Memory(tmp_path / "m.db"))
    sid = r.memory.new_session()

    async def fake_session():
        return FakeClient(script(words)), sid
    r._ensure_session = fake_session
    return r


async def collect(r, **kw):
    seen = []
    async def watch():
        async for e in r.bus.subscribe():
            seen.append((e.type, e.data))
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    events = [e async for e in r.stream_turn("hello", **kw)]
    await asyncio.sleep(0.02); w.cancel()
    return events, seen


async def test_with_voice_the_runner_does_not_announce_the_full_answer_itself(tmp_path):
    r = runner(tmp_path, ["The first ", "real result ", "is the official video."])
    events, seen = await collect(r, publish_result=False)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "The first real result is the official video."
    assert isinstance(events[-1], Finished) and events[-1].text.startswith("The first real result")
    assert not [d for t, d in seen if t == "result"]              # nothing for the screen to flash while the voice is still reading


async def test_without_voice_the_default_still_announces_the_result(tmp_path):
    r = runner(tmp_path, ["Hello there."])
    events, seen = await collect(r)
    assert [d["text"] for t, d in seen if t == "result"] == ["Hello there."]


async def test_prometheus_publishes_the_full_answer_only_after_the_voice_finished(tmp_path):
    from voicectl.modes.base import ModeContext
    from voicectl.modes.prometheus import PrometheusMode
    cfg = Config({"tts": {"enabled": True}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False},
                  "browser": {"backend": "playwright"}})
    bus = EventBus()
    order = []

    class Spk:
        active = False; spoken = []; reference = None
        async def speak_many(self, chunks, **kw):
            async for c in chunks:
                order.append(("spoken", c)); await asyncio.sleep(0.01)
        async def speak(self, t): ...
        def stop(self): ...
        def wake(self): ...
        def quiet_for(self): return 99.0
    ctx = ModeContext(cfg, bus, Spk())
    r = runner(tmp_path, ["The first real result. ", "It has two billion views."])
    r.bus = bus
    ctx.shared["agent_runner"] = r
    m = PrometheusMode(ctx)
    orig = bus.publish
    bus.publish = lambda t, **d: (order.append(("event", t)) if t == "result" else None) or orig(t, **d)
    await m._agent_turn("what is it")
    kinds = [k for k, _ in order]
    assert kinds.index("event") > max(i for i, k in enumerate(kinds) if k == "spoken")        # the full text lands after the last spoken sentence
