import asyncio

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.wake import WakeListener, find_wake


def test_find_wake_variants():
    assert find_wake("Prometheus") == (True, "")
    assert find_wake("Hey Prometheus, open Safari.") == (True, "open Safari.")
    assert find_wake("prometheus what's the weather") == (True, "what's the weather")
    assert find_wake("Promethius open my resume")[0]            # mis-transcription
    assert find_wake("Okay, Prometheus")[0]


def test_not_triggered_by_mentions_or_noise():
    assert not find_wake("I was reading about Prometheus the titan")[0]     # name not at the start
    assert not find_wake("open the pod bay doors")[0]
    assert not find_wake("")[0]
    assert not find_wake("Thank you.")[0]


SR = 16000
BLOCK = SR // 30


def tone(a):
    return (np.sin(np.linspace(0, 200, BLOCK)) * a).astype("float32")


class FakeMic:
    sample_rate = SR
    on_level = None

    def __init__(self, script):
        self.script, self.i, self.running = script, 0, False

    def start(self): self.running, self.i = True, 0; type(self).starts += 1
    def stop(self): self.running = False; return np.zeros(0, "float32")

    def blocks_since(self, idx):
        # reveal the scripted audio gradually, like a live stream
        self.i = min(len(self.script), self.i + 6)
        return self.script[idx:self.i], self.i

    def blocks_from(self, sample_index):
        self.i = min(len(self.script), self.i + 6)
        return self.script[sample_index // BLOCK:self.i], self.i * BLOCK

    starts = 0
    def trim_before(self, index): pass


class FakeSTT:
    def __init__(self, text): self.text, self.hints = text, []
    def transcribe(self, audio, sr, hint=None): self.hints.append(hint); return self.text


async def test_wake_fires_with_rest_of_utterance():
    script = [tone(0.002)] * 12 + [tone(0.25)] * 25 + [tone(0.002)] * 40
    woke = []
    stt = FakeSTT("Hey Prometheus, open Safari")
    w = WakeListener(Config({"wake_word": {"enabled": True, "word": "prometheus"}}), EventBus(), FakeMic(script), stt,
                     lambda: True, lambda rest, heard: woke.append((rest, heard)))
    w.start()
    for _ in range(100):
        if woke:
            break
        await asyncio.sleep(0.05)
    w.stop()
    assert woke == [("open Safari", "Hey Prometheus, open Safari")]
    assert stt.hints[0] == "Prometheus"           # recognition is biased toward the name


async def test_no_wake_for_other_speech_and_releases_mic_when_busy():
    script = [tone(0.002)] * 12 + [tone(0.25)] * 25 + [tone(0.002)] * 40
    woke, busy = [], {"v": False}
    mic = FakeMic(script)
    w = WakeListener(Config({"wake_word": {"enabled": True}}), EventBus(), mic, FakeSTT("what time is it"),
                     lambda: not busy["v"], lambda rest, heard: woke.append(rest))
    w.start()
    await asyncio.sleep(0.6)
    assert not woke
    busy["v"] = True                              # another mode took over
    await asyncio.sleep(0.8)
    assert mic.running is False                   # mic released while busy
    w.stop()


def test_the_name_is_caught_however_it_gets_mangled_but_not_inside_other_sentences():
    from voicectl.core.wake import find_wake
    for heard in ["Prometheus", "Promethius", "Prometheous.", "pro me the us", "Pro metheus, open Gmail", "hey Prometheus", "Prometheus's", "Prometheas", "promethias"]:
        assert find_wake(heard)[0], heard
    assert find_wake("Prometheus open LinkedIn") == (True, "open LinkedIn")
    assert find_wake("pro me the us open it")[1] == "open it"
    for ignored in ["open the prometheus dashboard", "what time is it", "promise me you will", "pro tip: stay hydrated", "the next day"]:
        assert not find_wake(ignored)[0], ignored


def test_waking_by_name_gets_a_warm_varied_greeting_not_a_bare_yes():
    from voicectl.modes.conversation import GREETINGS, PHRASES, greeting
    seen = [greeting() for _ in range(30)]
    assert all(g in GREETINGS and g in PHRASES for g in seen)
    assert all(a != b for a, b in zip(seen, seen[1:])) and len(set(seen)) > 2
    assert not any(g.strip("?.!").lower() == "yes" for g in GREETINGS)


async def test_the_wake_mic_stays_open_while_waiting_so_there_is_no_deaf_gap():
    """Regression: the listener used to stop/start the mic every few seconds; speech starting in that gap was lost."""
    FakeMic.starts = 0
    script = [tone(0.002)] * 200 + [tone(0.25)] * 25 + [tone(0.002)] * 40            # 6+ s of quiet, then "Prometheus"
    woke = []
    w = WakeListener(Config({"wake_word": {"enabled": True, "word": "prometheus"}}), EventBus(), FakeMic(script), FakeSTT("Prometheus"),
                     lambda: True, lambda rest, heard: woke.append(heard))
    w.start()
    for _ in range(160):
        if woke:
            break
        await asyncio.sleep(0.05)
    w.stop()
    assert woke == ["Prometheus"] and FakeMic.starts == 1                            # one open stream the whole time


def test_mic_reads_by_absolute_position_even_after_trimming():
    import numpy as np
    from voicectl.input.mic import Mic
    m = Mic(16000)
    for i in range(10):
        m._frames.append(np.full(100, i, "float32")); m._total += 100
    blocks, nxt = m.blocks_from(0)
    assert len(blocks) == 10 and nxt == 1000
    m.trim_before(500)                                                               # drops whole blocks before sample 500
    blocks, nxt = m.blocks_from(700)
    assert nxt == 1000 and int(blocks[0][0]) == 7 and len(blocks) == 3
    blocks, nxt = m.blocks_from(1000)
    assert blocks == [] and nxt == 1000
