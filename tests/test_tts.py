import asyncio

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.tts.speaker import Speaker, clean_for_speech


def test_clean_markdown_and_urls():
    t = clean_for_speech("**Found 3 roles:**\n- [Stripe PM](https://stripe.com/jobs/1) in NYC\n- see https://x.com now")
    assert "*" not in t and "http" not in t and "Stripe PM" in t and "-" not in t.split()


def test_trims_to_whole_sentences():
    long = "This is a sentence about jobs. " * 40
    out = clean_for_speech(long, max_chars=200)
    assert len(out) <= 200 and out.endswith(".")


def test_enabled_per_mode():
    sp = Speaker(Config({"tts": {"enabled": True, "modes": {"agent": True, "speak": False}}}), EventBus())
    assert sp.enabled_for("agent") and not sp.enabled_for("speak") and not sp.enabled_for("action")


async def test_stop_cancels_speech(monkeypatch, tmp_path):
    bus = EventBus()
    sp = Speaker(Config({"tts": {"backend": "say"}}), bus)
    started = asyncio.Event()

    async def fake_synth(text, tmp):
        return tmp_path / "x.aiff"

    async def slow_play(path):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(sp, "_synth", fake_synth)
    monkeypatch.setattr(sp, "_play", slow_play)
    events = []

    async def collect():
        async for ev in bus.subscribe():
            events.append(ev.data.get("on"))
            if ev.data.get("on") is False:
                return
    c = asyncio.create_task(collect()); await asyncio.sleep(0)
    t = asyncio.create_task(sp.speak("hello there"))
    await started.wait()
    sp.stop()
    await asyncio.wait_for(asyncio.gather(t, c), 2)
    assert events == [True, False]
    assert not t.cancelled()       # stopping speech must not cancel the caller


async def test_streamed_chunks_play_in_order(monkeypatch, tmp_path):
    sp = Speaker(Config({"tts": {"backend": "say", "preroll_ms": 0}}), EventBus())
    played = []

    async def fake_synth(text, tmp):
        return tmp_path / text.replace(" ", "_")

    async def play(path):
        played.append(path.name)
    monkeypatch.setattr(sp, "_synth", fake_synth)
    monkeypatch.setattr(sp, "_play", play)

    async def chunks():
        for c in ["First one.", "Second one.", "Third."]:
            yield c
    await sp.speak_many(chunks())
    assert played == ["First_one.", "Second_one.", "Third."]


async def test_silent_leadin_only_after_idle(monkeypatch, tmp_path):
    from voicectl.tts import speaker as spk
    sp = Speaker(Config({"tts": {"backend": "say", "preroll_ms": 300}}), EventBus())
    played = []

    async def fake_synth(text, tmp): return tmp_path / "x.aiff"
    async def play(path):
        played.append(path.name)
        spk.Speaker._last_end = spk.time.monotonic()
    monkeypatch.setattr(sp, "_synth", fake_synth)
    monkeypatch.setattr(sp, "_play", play)
    monkeypatch.setattr(spk.Speaker, "CACHE", tmp_path)

    spk.Speaker._last_end = 0.0                       # output has been idle for a long time
    await sp.speak("Goodbye.")
    assert played == ["_silence_300.wav", "x.aiff"]   # device is woken first, so "Goodbye." isn't clipped
    played.clear()
    await sp.speak("Okay.")                           # moments later: already awake, no extra delay
    assert played == ["x.aiff"]


async def test_text_is_revealed_in_step_with_the_voice(monkeypatch, tmp_path):
    import json
    bus = EventBus()
    sp = Speaker(Config({"tts": {"backend": "say", "preroll_ms": 0}}), bus)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    audio.with_suffix(".json").write_text(json.dumps([0.0, 0.10, 0.20, 0.30]))   # four words, 100 ms apart

    async def fake_synth(text, tmp): return audio

    async def play(path): await asyncio.sleep(0.5)
    monkeypatch.setattr(sp, "_synth", fake_synth)
    monkeypatch.setattr(sp, "_play", play)
    seen = []

    async def collect():
        async for ev in bus.subscribe():
            if ev.type == "speech_text":
                seen.append(ev.data["text"])
    c = asyncio.create_task(collect()); await asyncio.sleep(0)

    async def chunks():
        yield "one two three four"
    await sp.speak_many(chunks(), show=True)
    await asyncio.sleep(0.05); c.cancel()
    assert seen[0] == "one" and "one two" in seen and "one two three" in seen   # grows word by word
    assert seen[-1] == "one two three four"
    assert seen == sorted(seen, key=len)                                         # never shrinks or jumps backwards


async def test_wake_plays_silence_in_the_background_only_after_idle(monkeypatch, tmp_path):
    from voicectl.tts import speaker as spk
    sp = Speaker(Config({"tts": {"backend": "say", "preroll_ms": 300}}), EventBus())
    monkeypatch.setattr(spk.Speaker, "CACHE", tmp_path)
    launched = []

    async def fake_exec(*args, **kw):
        launched.append(args)
        class P:
            async def wait(self): return 0
        return P()
    monkeypatch.setattr(spk.asyncio, "create_subprocess_exec", fake_exec)
    spk.Speaker._last_end = 0.0
    sp.warm_output(); await asyncio.sleep(0.05)
    assert len(launched) == 1 and launched[0][0] == "afplay"
    sp.warm_output(); await asyncio.sleep(0.05)
    assert len(launched) == 1                                         # already awake: nothing more to do
