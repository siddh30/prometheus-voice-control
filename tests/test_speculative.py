import asyncio

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes import assistant as assistant_mod
from voicectl.modes.assistant import AssistantMode
from voicectl.modes.base import ModeContext
from voicectl.modes.conversation import SentenceBuffer
from voicectl.modes.router import Route
import voicectl.actions as pkg
from voicectl.actions.base import ActionResult, actions

actions.discover(pkg)


async def gen(items, delay=0.0):
    for i in items:
        await asyncio.sleep(delay)
        yield i


async def test_sentence_buffer_replays_in_order_and_starts_immediately():
    started = []

    async def source():
        for s in ["one.", "two.", "three."]:
            started.append(s); yield s
    buf = SentenceBuffer(source())
    await asyncio.sleep(0.05)
    assert started == ["one.", "two.", "three."]                 # it ran ahead of any consumer
    assert [s async for s in buf] == ["one.", "two.", "three."]


async def test_sentence_buffer_cancel_stops_generation():
    produced = []

    async def source():
        for i in range(1000):
            produced.append(i); await asyncio.sleep(0.01); yield f"s{i}"
    buf = SentenceBuffer(source())
    await asyncio.sleep(0.05); buf.cancel(); n = len(produced)
    await asyncio.sleep(0.1)
    assert len(produced) <= n + 1


def make_mode(monkeypatch, route_result, route_delay=0.2):
    cfg = Config({"tts": {"enabled": False}, "assistant": {"speculative_chat": True}})
    m = AssistantMode(ModeContext(cfg, EventBus()))
    m.history = [{"role": "user", "content": "q"}]
    chat_started = []

    async def tokens():
        chat_started.append(True)
        for t in ["Sure, here is a long answer, ", "with several parts. ", "And more."]:
            yield t
    m._tokens = tokens

    async def fake_route(text, recent, config, deciders, job=None):
        await asyncio.sleep(route_delay)
        return route_result
    monkeypatch.setattr(assistant_mod, "route", fake_route)
    monkeypatch.setattr(assistant_mod, "fast_route", lambda text, job=None: None)
    return m, chat_started


async def test_chat_reply_is_already_generating_while_the_router_decides(monkeypatch):
    m, chat_started = make_mode(monkeypatch, Route("chat", how="llm"), route_delay=0.3)
    task = asyncio.create_task(m.respond("how are you"))
    await asyncio.sleep(0.1)
    assert chat_started                                           # the reply began before the router answered
    reply = await asyncio.wait_for(task, 2)
    assert "long answer" in reply


async def test_non_chat_route_throws_the_speculative_reply_away(monkeypatch):
    m, chat_started = make_mode(monkeypatch, Route("action", actions.get("mute"), {}, how="llm"))
    ran = []
    cls = actions.get("mute")
    monkeypatch.setattr(cls, "run", lambda self: ran.append("mute") or ActionResult(True, "Muted", "Muted."))
    reply = await asyncio.wait_for(m.respond("mute it"), 3)
    assert ran == ["mute"] and reply == "Muted"
    assert m.history == [{"role": "user", "content": "q"}]       # the guessed chat reply left no trace


async def test_cancelling_the_turn_cancels_the_background_reply(monkeypatch):
    m, _ = make_mode(monkeypatch, Route("chat", how="llm"), route_delay=5)
    alive = []

    async def tokens():
        try:
            for i in range(1000):
                await asyncio.sleep(0.01); alive.append(i); yield f"s{i}. "
        finally:
            alive.append("closed")
    m._tokens = tokens
    t = asyncio.create_task(m.respond("hi"))
    await asyncio.sleep(0.1)
    t.cancel(); await asyncio.gather(t, return_exceptions=True)
    n = len(alive); await asyncio.sleep(0.1)
    assert len(alive) <= n + 1
