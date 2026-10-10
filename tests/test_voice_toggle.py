import asyncio
import json

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.tts.speaker import Speaker


def speaker(tmp_path, tts=True):
    return Speaker(Config({"tts": {"enabled": tts, "backend": "say", "preroll_ms": 0}}), EventBus(), prefs_path=tmp_path / "prefs.json")


async def watch(bus, kinds):
    got = []
    async def go():
        async for ev in bus.subscribe():
            if ev.type in kinds: got.append((ev.type, ev.data))
    t = asyncio.create_task(go()); await asyncio.sleep(0)
    return got, t


async def test_text_only_makes_no_sound_but_still_feeds_the_captions(monkeypatch, tmp_path):
    sp = speaker(tmp_path); sp.set_voice(False)
    played = []
    async def play(path): played.append(path)
    async def synth(text, tmp): played.append("SYNTH")
    monkeypatch.setattr(sp, "_play", play); monkeypatch.setattr(sp, "_synth", synth)
    got, t = await watch(sp.bus, {"speech_text", "speaking"})

    async def chunks():
        for c in ["First sentence **bold**.", "Second one 🎵."]:
            yield c
    await sp.speak_many(chunks(), show=True)
    await sp.speak("Hello there")
    await asyncio.sleep(0.02); t.cancel()
    assert played == []                                                        # nothing synthesized, nothing played
    assert [d["text"] for k, d in got if k == "speech_text"] == ["First sentence bold.", "First sentence bold. Second one ."]   # captions still appear, cleaned
    assert not [1 for k, _ in got if k == "speaking"]                          # never "speaking": so the echo/deaf logic stays out of it
    assert sp.active is False and sp.enabled_for("prometheus") is False


async def test_turning_voice_off_cuts_off_what_is_being_said_and_tells_the_widget(tmp_path):
    sp = speaker(tmp_path)
    stopped = []
    sp.stop = lambda: stopped.append(1)
    got, t = await watch(sp.bus, {"voice"})
    sp.set_voice(False); sp.set_voice(True)
    await asyncio.sleep(0.02); t.cancel()
    assert stopped == [1]                                                      # only when turning OFF
    assert got == [("voice", {"on": False}), ("voice", {"on": True})]


def test_the_choice_is_remembered_between_launches(tmp_path):
    sp = speaker(tmp_path); sp.set_voice(False)
    assert json.loads((tmp_path / "prefs.json").read_text())["voice"] is False
    assert speaker(tmp_path).voice_on is False                                 # a new launch starts text-only, as you left it
    sp2 = speaker(tmp_path); sp2.set_voice(True)
    assert speaker(tmp_path).voice_on is True


def test_without_a_saved_choice_the_config_decides_and_a_broken_file_is_harmless(tmp_path):
    assert speaker(tmp_path, tts=True).voice_on is True and speaker(tmp_path, tts=False).voice_on is False
    (tmp_path / "prefs.json").write_text("{not json")
    assert speaker(tmp_path, tts=True).voice_on is True


async def test_you_can_say_it(tmp_path):
    from voicectl.modes.base import ModeContext
    from voicectl.modes.prometheus import VOICE_OFF, VOICE_ON, PrometheusMode
    for t in ["text only", "go text only", "switch to text mode", "mute your voice", "stop talking out loud", "be quiet", "stay silent", "please text mode"]:
        assert VOICE_OFF.match(t), t
    for t in ["speak to me", "talk out loud", "voice on", "turn your voice back on", "unmute your voice", "talk to me again"]:
        assert VOICE_ON.match(t), t
    for t in ["stop", "what is a text editor", "play some music", "talk to me about Kubernetes", "voice of the customer"]:
        assert not VOICE_OFF.match(t) and not VOICE_ON.match(t), t                  # ordinary requests are never mistaken for it
    cfg = Config({"tts": {"enabled": True}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}})
    sp = speaker(tmp_path)
    spoke = []
    async def say(t): spoke.append(t)
    sp.speak = say
    ctx = ModeContext(cfg, EventBus(), sp)
    class R: pending_approval = False; mode_name = ""
    ctx.shared["agent_runner"] = R()
    m = PrometheusMode(ctx)
    assert await m.intercept("text only") is True and sp.voice_on is False and spoke == []        # silent: it must not announce itself aloud
    assert await m.intercept("speak to me") is True and sp.voice_on is True and spoke == ["Voice is back on."]


async def test_a_new_widget_is_told_the_current_voice_state_on_connect(tmp_path):
    from voicectl.ui_bridge.server import UIBridge
    sp = speaker(tmp_path); sp.set_voice(False)
    bridge = UIBridge(EventBus(), "127.0.0.1", 0, hello={"hotkeys": {}}, snapshot=lambda: [{"type": "voice", "data": {"on": sp.voice_on}}])
    sent = []
    class WS:
        async def send(self, m): sent.append(json.loads(m))
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
    await bridge._handler(WS())
    assert {"type": "voice", "data": {"on": False}} in sent
