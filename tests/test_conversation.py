import asyncio

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.conversation import FAREWELLS, ConversationMode, farewell, is_goodbye, sentences


class FakeMic:
    sample_rate = 16000


class FakeEars:
    """Scripted listener: the test pushes finished utterances and can fire the 'you started talking' signal."""
    def __init__(self): self.q = asyncio.Queue(); self.stopped = False
    async def run(self):
        try:
            await asyncio.sleep(3600)
        finally:
            self.stopped = True
    async def next(self): return await self.q.get()
    def say(self, tag): self.q.put_nowait(tag)


class STT:
    """Audio is just a tag string here; transcribe returns it (so tests read like conversations)."""
    def transcribe(self, audio, sr): return audio


def make(respond, no_speech_s=30):
    cfg = Config({"tts": {"enabled": False}, "conversation": {"no_speech_s": no_speech_s, "output": "headphones"}, "wake_word": {"enabled": False}})
    bus = EventBus()
    m = ConversationMode(ModeContext(cfg, bus))
    m.respond = respond
    ears = FakeEars()
    m.ears_factory = lambda mic, transcribe=None: ears
    return m, ears, bus


async def start(m):
    return asyncio.create_task(m.run(FakeMic(), STT()))


async def settle(t=0.05): await asyncio.sleep(t)


async def test_goodbye_ends_and_releases_the_ears():
    async def respond(t): return "ok"
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)
    assert ears.stopped


async def test_a_normal_exchange_then_goodbye():
    seen = []
    async def respond(t): seen.append(t); return f"re: {t}"
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("hello there"); await settle()
    ears.say("how are you"); await settle()
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)
    assert seen == ["hello there", "how are you"]
    assert [h["role"] for h in m.history] == ["user", "assistant", "user", "assistant"]


async def test_hearing_you_start_talking_cuts_the_reply_off_immediately():
    """The moment the ears fire, the in-flight reply is cancelled, before the new sentence is even transcribed."""
    started, cancelled = asyncio.Event(), []
    async def respond(t):
        if t == "long one":
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(t); raise
        return "ok"
    m, ears, bus = make(respond)
    run = await start(m)
    ears.say("long one")
    await started.wait()
    m._on_speech_start()                         # what the ears do the instant you begin to talk
    await settle()
    assert cancelled == ["long one"] and not m._busy()
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)
    assert m.history[1]["content"] == "(interrupted by the user)"


async def test_new_utterance_replaces_the_running_reply():
    started = asyncio.Event()
    answered = []
    async def respond(t):
        if t == "first":
            started.set()
            await asyncio.sleep(30)
        answered.append(t)
        return f"re: {t}"
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("first"); await started.wait()
    ears.say("actually, second"); await settle(0.15)
    assert answered == ["actually, second"]
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)
    assert [h["content"] for h in m.history][:4] == ["first", "(interrupted by the user)", "actually, second", "re: actually, second"]


async def test_key_interrupt_only_when_busy():
    started = asyncio.Event()
    async def respond(t):
        started.set(); await asyncio.sleep(30)
    m, ears, _ = make(respond)
    assert m.interrupt() is False                # nothing running
    run = await start(m)
    ears.say("go"); await started.wait()
    assert m.interrupt() is True                 # busy: cut it off, conversation continues
    await settle()
    assert not m._busy() and not run.done()
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)


async def test_error_does_not_end_the_conversation_but_three_do():
    calls = []
    async def respond(t):
        calls.append(t)
        if len(calls) == 1:
            raise RuntimeError("API down")
        return "worked"
    m, ears, bus = make(respond)
    errs = []
    async def watch():
        async for ev in bus.subscribe():
            if ev.type == "error": errs.append(ev.data["message"])
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    run = await start(m)
    ears.say("one"); await settle(0.1)
    ears.say("two"); await settle(0.1)
    ears.say("goodbye")
    await asyncio.wait_for(run, 3); w.cancel()
    assert calls == ["one", "two"] and errs and [h["role"] for h in m.history] == ["user", "assistant"]

    async def always_fails(t): raise RuntimeError("down")
    m2, ears2, _ = make(always_fails)
    run2 = await start(m2)
    for tag in ("a", "b", "c"):
        ears2.say(tag); await settle(0.1)
    await asyncio.wait_for(run2, 3)              # ends itself after three in a row


async def test_two_quiet_windows_end_it_instead_of_hanging():
    async def respond(t): return "ok"
    m, ears, _ = make(respond, no_speech_s=0.5)
    run = await start(m)
    await asyncio.wait_for(run, 5)


async def test_unintelligible_audio_does_not_loop_forever():
    async def respond(t): return "ok"
    m, ears, _ = make(respond)
    run = await start(m)
    for _ in range(4):
        ears.say(""); await settle(0.1)
    await asyncio.wait_for(run, 3)


async def test_ending_by_key_cancels_everything():
    started = asyncio.Event()
    async def respond(t):
        started.set(); await asyncio.sleep(30)
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("go"); await started.wait()
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert ears.stopped and not m._busy()


async def test_the_farewell_is_not_cut_off_by_the_ears():
    """Regression: the ears keep running during "Goodbye." and must not cancel it."""
    spoken = []
    cfg = Config({"tts": {"enabled": True}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}})
    bus = EventBus()
    m = ConversationMode(ModeContext(cfg, bus))

    class Spk:
        active = False
        first_audio_at = 0.0
        def wake(self): pass
        async def speak(self, text):
            spoken.append(("start", text))
            m._on_speech_start()                  # the ears fire while it is talking (e.g. echo)
            await asyncio.sleep(0.1)
            spoken.append(("end", text))
        def stop(self): spoken.append(("stop",))
    m.ctx.speaker = Spk()
    async def respond(t): return "ok"
    m.respond = respond
    ears = FakeEars(); m.ears_factory = lambda mic, transcribe=None: ears
    run = await start(m)
    await settle(0.3)                              # the "I'm listening." ack finished
    spoken.clear()
    ears.say("goodbye")
    await asyncio.wait_for(run, 3)
    said = next(t for kind, *rest in spoken if kind == "start" for t in rest)     # the farewell
    assert said in FAREWELLS and ("end", said) in spoken            # it finished saying goodbye
    assert ("stop",) not in spoken[spoken.index(("start", said)):]


def test_goodbye_phrases():
    assert is_goodbye("Goodbye.") and is_goodbye("that's all") and is_goodbye("Stop listening")
    assert not is_goodbye("goodbye cruel world is a great song")


async def test_sentence_splitter():
    async def toks():
        for t in ["Hello th", "ere. How ar", "e you? Fine"]:
            yield t
    assert [s async for s in sentences(toks())] == ["Hello there.", "How are you?", "Fine"]


async def test_first_clause_is_released_early_for_a_faster_first_word():
    async def toks():
        for t in ["Well, that depends on what you mean by good, ", "so let me explain. Next one."]:
            yield t
    out = [s async for s in sentences(toks())]
    assert out[0] == "Well, that depends on what you mean by good,"    # spoken before the sentence is even finished
    assert out[1:] == ["so let me explain.", "Next one."]


async def test_short_openers_are_not_chopped():
    async def toks():
        yield "Sure, I can help. Anything else?"
    assert [s async for s in sentences(toks())] == ["Sure, I can help.", "Anything else?"]


async def test_state_is_not_repeated_so_the_widget_cannot_blink():
    async def respond(t): return "ok"
    m, ears, bus = make(respond)
    states = []
    async def watch():
        async for ev in bus.subscribe():
            if ev.type == "state": states.append(ev.data["state"])
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    run = await start(m)
    ears.say("hello"); await settle(0.1)
    m._on_speech_start(); m._on_speech_start()                 # repeated signals
    ears.say("goodbye")
    await asyncio.wait_for(run, 3); w.cancel()
    assert all(a != b for a, b in zip(states, states[1:])), states


async def test_a_cancelled_reply_does_not_announce_listening_over_its_replacement():
    started = asyncio.Event()
    async def respond(t):
        if t == "first":
            started.set(); await asyncio.sleep(30)
        await asyncio.sleep(0.2)
        return "ok"
    m, ears, bus = make(respond)
    states = []
    async def watch():
        async for ev in bus.subscribe():
            if ev.type == "state": states.append(ev.data["state"])
    w = asyncio.create_task(watch()); await asyncio.sleep(0)
    run = await start(m)
    ears.say("first"); await started.wait(); await settle(0.05)
    states.clear()
    ears.say("second"); await settle(0.1)
    assert "listening" not in states[:states.index("thinking") if "thinking" in states else None], states
    ears.say("goodbye")
    await asyncio.wait_for(run, 3); w.cancel()


def test_openai_backend_forces_english(monkeypatch):
    import numpy as np, sys, types
    from voicectl.core.config import Config
    from voicectl.stt.openai_api import OpenAIBackend
    seen = {}
    class T:
        def create(self, **kw): seen.update(kw); return types.SimpleNamespace(text=" hi ")
    fake = types.SimpleNamespace(OpenAI=lambda: types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=T())))
    monkeypatch.setitem(sys.modules, "openai", fake)
    assert OpenAIBackend(Config({"stt": {}})).transcribe(np.zeros(1600, "float32"), 16000) == "hi"
    assert seen["language"] == "en"


def test_every_natural_way_of_closing_ends_the_conversation_but_requests_do_not():
    from voicectl.modes.conversation import is_goodbye
    ends = ["bye", "Goodbye.", "thanks", "Thank you!", "thank you so much", "thanks a lot Prometheus", "that's all", "that's all, thank you",
            "okay I'm done", "we're done here", "you're done", "see you later", "talk to you later", "good night", "cheers", "I appreciate it",
            "that'll be all", "nothing else", "alright that's it for now", "stop listening", "never mind", "ok thanks bye", "that is all thanks",
            "you can stop now", "I'm good", "all set, thanks", "thanks Prometheus, that's everything"]
    keeps = ["thanks, now open LinkedIn", "thank you, play some music", "what's the weather", "stop the music and open Gmail",
             "I'm done with the resume, can you email it", "bye the way what time is it", "search for good night songs"]
    assert [t for t in ends if not is_goodbye(t)] == []
    assert [t for t in keeps if is_goodbye(t)] == []


def test_transcripts_that_are_not_speech_are_dropped():
    from voicectl.stt.openai_api import clean_transcript as c
    assert c("Thanks for watching!") == "" and c("Please subscribe to my channel") == "" and c("کورسر ٹیب۔") == "" and c("Јуверанда корсар таб.") == ""
    assert c("Prometheus", hint="Prometheus") == "" and c("[Music]") == ""
    assert c("Can you open Obvious by Chris Brown?") == "Can you open Obvious by Chris Brown?"
    assert c("thank you") == "thank you" and c("bye") == "bye" and c("Prometheus open Gmail", hint="Prometheus") == "Prometheus open Gmail"
    assert c("Café near Zürich") == "Café near Zürich"


def test_farewells_are_warm_varied_and_fit_what_was_said():
    from voicectl.modes.conversation import IDLE_FAREWELLS, NIGHT_REPLIES, PHRASES, THANKS_REPLIES
    assert farewell("thank you so much") in THANKS_REPLIES and farewell("good night") in NIGHT_REPLIES
    assert farewell("bye") in FAREWELLS and farewell() in IDLE_FAREWELLS
    seen = [farewell("bye") for _ in range(30)]
    assert all(a != b for a, b in zip(seen, seen[1:])) and len(set(seen)) > 2
    assert all(f in PHRASES for f in [*FAREWELLS, *THANKS_REPLIES, *NIGHT_REPLIES, *IDLE_FAREWELLS])
    assert not any(f.strip(".!").lower() == "goodbye" for f in FAREWELLS)


async def test_saying_bye_goes_idle_at_once_even_if_the_agent_is_slow_to_stop():
    import time
    done = asyncio.Event()
    async def respond(t):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(5)                 # an agent that takes seconds to wind down
            raise
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("do something long"); await settle(0.1)
    t0 = time.monotonic()
    ears.say("bye")
    await asyncio.wait_for(run, 3)
    assert time.monotonic() - t0 < 2.0


async def test_a_typed_message_interrupts_the_current_reply_and_becomes_the_newest_instruction():
    seen, cancelled = [], []
    async def respond(t):
        seen.append(t)
        if t == "first":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(t); raise
        return f"re: {t}"
    m, ears, _ = make(respond)
    run = await start(m)
    ears.say("first"); await settle(0.1)
    await m.submit_text("  second, typed  ")
    await settle(0.2)
    assert cancelled == ["first"] and seen == ["first", "second, typed"]
    await m.submit_text("thanks, bye")                       # typing a goodbye ends the conversation like saying it
    await asyncio.wait_for(run, 3)
