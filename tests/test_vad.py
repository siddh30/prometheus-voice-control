import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from voicectl.core.vad import VadEndpointer

SR = 16000


@pytest.fixture(scope="module")
def speech():
    with tempfile.TemporaryDirectory() as d:
        aiff, raw = Path(d) / "s.aiff", Path(d) / "s.raw"
        try:
            subprocess.run(["say", "-o", str(aiff), "Right, within LinkedIn, can you help me find jobs please?"], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(aiff), "-ar", "16000", "-ac", "1", "-f", "f32le", str(raw)], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("needs macOS say + ffmpeg")
        return np.fromfile(raw, dtype="float32")


def noise(sec, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(0, 0.01, int(SR * sec)).astype("float32")


def clicks(sec):
    a = noise(sec, 1)
    for i in range(0, len(a) - 300, SR // 3):
        a[i:i + 300] += np.random.default_rng(i).normal(0, 0.3, 300).astype("float32")
    return a


def stream(vad, audio, step=0.2):
    """Feed audio like a live mic would; return (verdict, seconds at which it fired)."""
    n = 0
    while n < len(audio):
        n = min(len(audio), n + int(SR * step))
        v = vad.update(audio[:n][-8 * SR:], n)
        if v != "continue":
            return v, n / SR
    return "continue", n / SR


def test_ends_shortly_after_speech_even_with_clicks_after(speech):
    """The energy detector failed exactly here: sporadic clicks kept resetting the silence timer for ~20 s."""
    audio = np.concatenate([noise(0.5), speech, clicks(6)])
    v, at = stream(VadEndpointer(SR, silence_s=0.9), audio)
    speech_end = 0.5 + len(speech) / SR
    assert v == "done" and speech_end < at < speech_end + 2.0


def test_clicks_and_noise_alone_are_not_speech():
    v, _ = stream(VadEndpointer(SR, no_speech_s=3), np.concatenate([noise(2), clicks(6)]))
    assert v == "timeout"


def test_keeps_listening_during_a_pause_mid_sentence(speech):
    half = len(speech) // 2
    audio = np.concatenate([speech[:half], noise(0.5), speech[half:], noise(3)])
    v, at = stream(VadEndpointer(SR, silence_s=1.2), audio)
    assert v == "done" and at > len(speech) / SR      # did not cut the user off during the 0.5 s pause


def test_loudness_gate_ignores_quiet_echo(speech):
    """While the assistant talks, its own (quieter) echo must not count as the user barging in."""
    quiet = (speech * 0.05).astype("float32")                  # speech-like but faint, like an echo
    loud = speech
    level = float(np.sqrt(np.mean(loud ** 2)))
    ungated = VadEndpointer(SR, silence_s=0.9)
    ungated.update(np.concatenate([noise(0.3), quiet])[-8 * SR:], len(noise(0.3)) + len(quiet))
    gated_quiet = VadEndpointer(SR, silence_s=0.9)
    gated_quiet.update(np.concatenate([noise(0.3), quiet]), len(noise(0.3)) + len(quiet), gate=level * 0.5)
    gated_loud = VadEndpointer(SR, silence_s=0.9)
    gated_loud.update(np.concatenate([noise(0.3), loud]), len(noise(0.3)) + len(loud), gate=level * 0.5)
    assert ungated.heard_speech            # faint speech is still speech to the model
    assert not gated_quiet.heard_speech    # but not once we gate on loudness
    assert gated_loud.heard_speech


async def test_ears_with_the_real_detector_catch_two_utterances_in_a_row(speech):
    """End to end on real speech: the ears must split a continuous stream into two separate utterances, fire
    on_speech_start while each is still being spoken, and never lose the second one."""
    import asyncio
    from voicectl.core.ears import Ears

    stream = np.concatenate([noise(1.0), speech, noise(2.0), speech, noise(2.0)]).astype("float32")

    class StreamMic:
        sample_rate = SR
        def __init__(self): self.total = 0
        def start(self): pass
        def stop(self): pass
        def tail(self, n):
            self.total = min(len(stream), self.total + SR // 4)       # 0.25 s of new audio per poll
            return stream[max(0, self.total - n):self.total], self.total
        def slice(self, a, b): return stream[a:b]
        def trim_before(self, i): pass

    mic, starts = StreamMic(), []
    ears = Ears(mic, lambda base: VadEndpointer(SR, silence_s=0.8, no_speech_s=1e9, base_sample=base),
                lambda: False, on_speech_start=lambda: starts.append(mic.total), poll_s=0.001)
    task = asyncio.create_task(ears.run())
    first = await asyncio.wait_for(ears.next(), 20)
    second = await asyncio.wait_for(ears.next(), 20)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    speech_len = len(speech)
    for u in (first, second):
        assert 0.8 * speech_len < len(u.audio) < 1.5 * speech_len            # each is one utterance, not both merged, not clipped
    assert len(starts) == 2 and starts[0] < 1.0 * SR + speech_len and starts[1] < 3.0 * SR + 2 * speech_len
