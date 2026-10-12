import asyncio

import numpy as np

from voicectl.channels import TextChannel, VoiceChannel
from voicectl.channels.voice import PushRecorder
from voicectl.core.config import Config
from voicectl.core.events import EventBus


class FakeMic:
    sample_rate = 16000

    def __init__(self, audio=None, fail=False):
        self.audio = np.full(16000, 0.2, "float32") if audio is None else audio
        self.fail, self.started, self.stopped = fail, 0, 0

    def start(self):
        if self.fail:
            raise OSError("no input device")
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.audio


def recorder(mic, **kw):
    now = kw.pop("now", [100.0])
    events = {"start": 0, "discard": 0, "problems": []}
    r = PushRecorder(mic, on_start=lambda: events.__setitem__("start", events["start"] + 1),
                     on_discard=lambda: events.__setitem__("discard", events["discard"] + 1),
                     on_problem=events["problems"].append, clock=lambda: now[0], **kw)
    return r, events, now


async def test_press_starts_recording_and_the_next_press_sends_it():
    mic = FakeMic()
    r, ev, now = recorder(mic)
    r.press()
    assert r.recording and mic.started == 1 and ev["start"] == 1                  # cut off the reply, then open the microphone
    now[0] += 2.0
    r.press()
    assert not r.recording and mic.stopped == 1
    heard = await asyncio.wait_for(r.next(), 1)
    assert heard.audio.size == 16000 and heard.speech_end_at == now[0]


async def test_a_press_that_is_too_short_is_discarded_with_a_plain_hint():
    r, ev, now = recorder(FakeMic())
    r.press()
    now[0] += 0.1
    r.press()
    assert ev["problems"] == ["too_short"] and ev["discard"] == 1 and r._queue.empty()


async def test_an_empty_recording_and_a_silent_one_are_reported_differently():
    r, ev, now = recorder(FakeMic(audio=np.zeros(0, "float32")))
    r.press(); now[0] += 2; r.press()
    assert ev["problems"] == ["no_audio"] and ev["discard"] == 1
    r2, ev2, now2 = recorder(FakeMic(audio=np.zeros(16000, "float32")))
    r2.press(); now2[0] += 2; r2.press()
    assert ev2["problems"] == ["silent"] and not r2._queue.empty()                # still sent: the transcript decides


async def test_a_microphone_that_cannot_open_is_reported_and_never_left_recording():
    r, ev, _ = recorder(FakeMic(fail=True))
    r.press()
    assert not r.recording and ev["problems"] == ["unavailable"] and ev["discard"] == 1


async def test_a_forgotten_recording_ends_itself():
    r, ev, now = recorder(FakeMic(), max_s=0.05)
    r.press()
    now[0] += 2
    await asyncio.sleep(0.2)
    assert not r.recording and not r._queue.empty()


async def test_typing_mode_ignores_presses_and_drops_a_recording_in_progress():
    mic = FakeMic()
    v = VoiceChannel(Config({}), EventBus(), None, on_start=lambda: None)
    v.make_recorder = lambda m: PushRecorder(m, clock=lambda: 1.0)
    r = v.open(mic)
    v.press()
    assert v.recording and mic.started == 1
    v.pause(True)                                                                # the type box opens
    assert not v.recording and mic.stopped == 1 and r._queue.empty()             # dropped, not sent
    v.press()
    assert mic.started == 1                                                      # presses do nothing while typing
    v.pause(False)
    v.press()
    assert v.recording and mic.started == 2                                      # closing the box brings voice back


def test_the_voice_channel_stands_alone_with_no_conversation_or_agent():
    events = []
    bus = EventBus()
    bus.publish = lambda t, **d: events.append((t, d))
    v = VoiceChannel(Config({}), bus, None, on_start=lambda: None)
    v.on_mic_problem("silent")
    v.on_mic_problem("unavailable")
    assert [t for t, _ in events] == ["notice", "error"]
    assert "Microphone" in events[0][1]["text"] and "connected" in events[1][1]["message"]


def test_typing_silences_mic_warnings_through_the_text_channel_only():
    now = [1000.0]
    text = TextChannel(clock=lambda: now[0])
    events = []
    bus = EventBus()
    bus.publish = lambda t, **d: events.append(t)
    v = VoiceChannel(Config({}), bus, None, on_start=lambda: None, quiet=text.recently_used)
    text.note_typed()
    v.on_mic_problem("silent")
    v.on_mic_problem("too_short")
    assert events == []
    now[0] += 301                                                                # five minutes without typing
    v.on_mic_problem("silent")
    assert events == ["notice"]
