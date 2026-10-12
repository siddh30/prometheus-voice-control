import asyncio

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.registry import Registry
from voicectl.core.session import SessionManager, State
from voicectl.modes.base import Mode, ModeContext


class FakeMic:
    sample_rate = 16000
    on_level = None
    def start(self): pass
    def stop(self): return np.ones(16000, dtype="float32")


class FakeSTT:
    def transcribe(self, audio, sr): return "hello world"


class RecordingMode(Mode):
    name = "speak"
    got: list[str] = []
    async def handle(self, text): self.got.append(text)


def make(min_ms=0, rec_mode="hold"):
    cfg = Config({"recording": {"min_hold_ms": min_ms, "max_hold_s": 120, "mode": rec_mode}})
    bus = EventBus()
    mode = RecordingMode(ModeContext(cfg, bus))
    mode.got = []
    return SessionManager(cfg, bus, FakeMic(), FakeSTT(), {"speak": mode}), mode, bus


async def test_hold_release_dispatches_transcript():
    s, mode, _ = make()
    s.key_down("speak")
    assert s.state is State.LISTENING
    s.key_up("speak")
    await asyncio.sleep(0.05)
    assert mode.got == ["hello world"]
    assert s.state is State.IDLE


async def test_short_tap_ignored():
    s, mode, _ = make(min_ms=300)
    s.key_down("speak")
    s.key_up("speak")
    await asyncio.sleep(0.05)
    assert mode.got == [] and s.state is State.IDLE


async def test_second_mode_key_ignored_while_held():
    s, _, _ = make()
    s.modes["agent"] = s.modes["speak"]
    s.key_down("speak")
    s.key_down("agent")
    assert s.active_mode == "speak"


async def test_cancel():
    s, mode, _ = make()
    s.key_down("speak")
    s.cancel()
    s.key_up("speak")
    await asyncio.sleep(0.05)
    assert mode.got == [] and s.state is State.IDLE


def test_registry_duplicate_and_unknown():
    r = Registry("x")
    r.register("a")(type("A", (), {}))
    try:
        r.register("a")(type("B", (), {}))
        assert False
    except ValueError:
        pass
    try:
        r.get("zzz")
        assert False
    except KeyError:
        pass


async def test_toggle_press_start_press_stop():
    s, mode, _ = make(rec_mode="toggle")
    s.key_down("speak"); s.key_up("speak")          # release must not stop it
    assert s.state is State.LISTENING
    s.key_down("speak"); s.key_up("speak")          # second press stops
    await asyncio.sleep(0.05)
    assert mode.got == ["hello world"] and s.state is State.IDLE


async def test_toggle_other_mode_key_ignored_while_listening():
    s, mode, _ = make(rec_mode="toggle")
    s.modes["agent"] = s.modes["speak"]
    s.key_down("speak"); s.key_down("agent")
    assert s.state is State.LISTENING and s.active_mode == "speak"


async def test_toggle_cancel():
    s, mode, _ = make(rec_mode="toggle")
    s.key_down("speak"); s.cancel()
    await asyncio.sleep(0.05)
    assert mode.got == [] and s.state is State.IDLE


async def test_level_callback_from_audio_thread_publishes():
    """Regression: the mic calls on_level from its own thread; this once raised and killed the recording."""
    import threading
    s, _, bus = make()
    got = []

    async def collect():
        async for ev in bus.subscribe():
            got.append(ev)
            return
    t = asyncio.create_task(collect())
    await asyncio.sleep(0)
    threading.Thread(target=s._on_level, args=(0.5,)).start()
    await asyncio.wait_for(t, 1)
    assert got[0].type == "level" and got[0].data["value"] == 0.5


class HangingConversation(Mode):
    name = "assistant"
    continuous = True
    started = 0
    presses = 0
    def press(self): HangingConversation.presses += 1
    async def handle(self, text): ...
    async def run(self, mic, stt, initial=""):
        HangingConversation.started += 1
        await asyncio.sleep(60)               # simulates a conversation stuck waiting


async def test_any_mode_key_escapes_a_stuck_conversation():
    """Regression: while a conversation was running, other hotkeys were silently ignored, leaving the user stuck."""
    cfg = Config({"recording": {"min_hold_ms": 0, "max_hold_s": 120, "mode": "toggle"}})
    bus = EventBus()
    conv = HangingConversation(ModeContext(cfg, bus))
    speak = RecordingMode(ModeContext(cfg, bus)); speak.got = []
    s = SessionManager(cfg, bus, FakeMic(), FakeSTT(), {"assistant": conv, "speak": speak})
    s.key_down("assistant")
    await asyncio.sleep(0.05)
    assert s.state is State.HANDLING
    s.key_down("speak")                       # F9 during the conversation
    await asyncio.sleep(0.8)
    assert s.state is State.LISTENING and s.active_mode == "speak"     # conversation ended, dictation started
    s.key_down("speak")
    await asyncio.sleep(0.1)
    assert speak.got == ["hello world"]


async def test_the_same_key_inside_a_conversation_starts_and_sends_recordings_and_escape_ends_it():
    cfg = Config({"recording": {"min_hold_ms": 0, "mode": "toggle"}})
    bus = EventBus()
    HangingConversation.presses = 0
    s = SessionManager(cfg, bus, FakeMic(), FakeSTT(), {"assistant": HangingConversation(ModeContext(cfg, bus))})
    s.key_down("assistant"); await asyncio.sleep(0.05)           # opens the conversation (its first recording starts there)
    assert s.conversation_active() and HangingConversation.presses == 0
    s.key_down("assistant"); await asyncio.sleep(0.05)           # F10 again: the recorder's start / send
    assert s.conversation_active() and HangingConversation.presses == 1 and s.state is State.HANDLING
    s.cancel(); await asyncio.sleep(0.05)                        # Escape ends the conversation
    assert s.state is State.IDLE and not s.conversation_active()


async def test_typing_can_start_a_conversation_without_recording_anything():
    cfg = Config({"recording": {"min_hold_ms": 0, "mode": "toggle"}})
    bus = EventBus()
    conv = HangingConversation(ModeContext(cfg, bus))
    s = SessionManager(cfg, bus, FakeMic(), FakeSTT(), {"assistant": conv})
    s.start_with_text("assistant", "open linkedin")
    await asyncio.sleep(0.05)
    assert s.conversation_active() and s.state is State.HANDLING


async def test_a_microphone_that_cannot_open_is_reported_not_left_listening():
    from voicectl.core.session import SessionManager, State
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    class DeadMic:
        sample_rate = 16000
        def start(self): raise OSError("no input device")
        def stop(self): return None
    bus = EventBus(); seen = []
    mode = RecordingMode(ModeContext(Config({}), bus))
    sm = SessionManager(Config({}), bus, DeadMic(), None, {"speak": mode}, None, None)
    sm.key_down("speak")
    assert sm.state is State.IDLE
