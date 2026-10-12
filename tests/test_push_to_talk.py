"""The whole F10 path with the real session manager and conversation loop; only the microphone, STT and the reply are stubs."""
import asyncio

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.session import SessionManager, State
from voicectl.modes.base import ModeContext
from voicectl.modes.conversation import ConversationMode


class Mic:
    sample_rate = 16000
    on_level = None

    def __init__(self): self.open, self.opened = False, 0
    def start(self): self.open = True; self.opened += 1
    def stop(self): self.open = False; return np.full(16000, 0.2, "float32")


class STT:
    def __init__(self, *words): self.words = list(words)
    def transcribe(self, audio, sr): return self.words.pop(0) if self.words else ""


def build(*words, respond=None):
    cfg = Config({"tts": {"enabled": False}, "recording": {"min_hold_ms": 0, "mode": "toggle"}})
    bus, states = EventBus(), []
    orig = bus.publish
    bus.publish = lambda t, **d: (states.append(d["state"]) if t == "state" else None, orig(t, **d))[1]
    mode = ConversationMode(ModeContext(cfg, bus))
    heard = []
    async def default(text): heard.append(text); return f"re: {text}"
    mode.respond = respond or default
    mic = Mic()
    session = SessionManager(cfg, bus, mic, STT(*words), {"conversation": mode})
    return session, mode, mic, states, heard, bus


async def settle(t=0.2): await asyncio.sleep(t)


async def test_f10_records_f10_sends_and_the_mic_is_closed_the_rest_of_the_time():
    session, mode, mic, states, heard, bus = build("open linkedin")
    session.key_down("conversation"); await settle()
    assert mic.open and mic.opened == 1 and states[-1] == "listening"      # the first press opened the conversation AND started recording
    session.key_down("conversation"); await settle(0.5)                    # the second press sends it
    assert not mic.open                                                    # closed again: nothing is listening
    assert heard == ["open linkedin"] and states[-1] == "idle"             # answered, now waiting for the next press
    assert session.conversation_active()                                   # the conversation stays open
    session.cancel(); await settle()


async def test_pressing_f10_while_it_is_working_cuts_it_off_and_records_the_next_request():
    gate = asyncio.Event()
    cancelled = []
    async def respond(text):
        if text == "long task":
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.append(text); raise
        return f"re: {text}"
    session, mode, mic, states, heard, bus = build("long task", "something else", respond=respond)
    session.key_down("conversation"); await settle()
    session.key_down("conversation"); await settle()                       # "long task" is now running
    assert mode._busy() and not mic.open
    session.key_down("conversation"); await settle()                       # F10 again: interrupt and record
    assert cancelled == ["long task"] and mic.open and states[-1] == "listening"
    session.key_down("conversation"); await settle(0.5)                    # send the new request
    assert not mode._busy() and states[-1] == "idle"
    session.cancel(); await settle()


async def test_typing_and_pressing_enter_interrupts_and_never_opens_the_microphone():
    gate = asyncio.Event()
    async def respond(text):
        if text == "first":
            await gate.wait()
        return f"re: {text}"
    session, mode, mic, states, heard, bus = build("first", respond=respond)
    session.key_down("conversation"); await settle()
    session.key_down("conversation"); await settle()                       # "first" is running
    opened = mic.opened
    mode.set_typed_only(True)                                              # the type box opens
    await mode.submit_text("actually, second")                             # Enter
    await settle(0.3)
    assert mic.opened == opened and not mic.open                           # no recording happened
    session.key_down("conversation"); await settle()
    assert mic.opened == opened                                            # F10 is ignored while typing
    mode.set_typed_only(False)
    session.key_down("conversation"); await settle()
    assert mic.opened == opened + 1 and mic.open                           # closing the box brings voice back
    session.cancel(); await settle()


async def test_an_empty_recording_says_so_and_keeps_waiting():
    session, mode, mic, states, heard, bus = build("")
    notices = []
    orig = bus.publish
    bus.publish = lambda t, **d: (notices.append(d.get("text")) if t == "notice" else None, orig(t, **d))[1]
    session.key_down("conversation"); await settle()
    session.key_down("conversation"); await settle(0.5)
    assert any("Didn't catch that" in (n or "") for n in notices) and heard == []
    assert states[-1] == "idle" and session.conversation_active()
    session.cancel(); await settle()


async def test_escape_ends_the_conversation_and_releases_the_microphone():
    session, mode, mic, states, heard, bus = build("hello")
    session.key_down("conversation"); await settle()
    assert mic.open
    session.cancel(); await settle()
    assert not mic.open and not session.conversation_active() and session.state is State.IDLE
