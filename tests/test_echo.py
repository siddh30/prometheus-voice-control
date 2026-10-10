"""Synthetic acoustics: real speech as the assistant's voice, a delayed/reverberant/clipped copy as the echo in the mic, and a
second voice as the user talking over it. No hardware needed."""
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from voicectl.core.echo import FRAME_S, SR, EchoReference, TalkOverDetector

ASSISTANT = ("I found three product manager roles that fit your resume. The first is at Stripe, the second is at Datadog, "
             "and the third is at a smaller fintech startup in New York. Let me tell you a little about each one of them.")
USER = "Actually wait, stop. Open the second one instead and tell me about the salary range."


def tts(text, voice):
    with tempfile.TemporaryDirectory() as d:
        a, r = Path(d) / "s.aiff", Path(d) / "s.raw"
        try:
            subprocess.run(["say", "-v", voice, "-o", str(a), text], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(a), "-ar", "16000", "-ac", "1", "-f", "f32le", str(r)], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("needs macOS say + ffmpeg")
        x = np.fromfile(r, dtype="float32")
    return x / (np.abs(x).max() + 1e-9) * 0.5


@pytest.fixture(scope="module")
def voices():
    return tts(ASSISTANT, "Daniel"), tts(USER, "Samantha")


def room(ref_pcm, delay_s, gain, reverb_s, clip=0.0, seed=0):
    rng = np.random.default_rng(seed)
    n = int(reverb_s * SR)
    h = np.zeros(int(delay_s * SR) + n + 1)
    h[int(delay_s * SR)] = 1.0
    if n:
        h[int(delay_s * SR) + 1:] = rng.normal(0, 1, n) * np.exp(-np.arange(1, n + 1) / (0.35 * n)) * 0.25
    echo = np.convolve(ref_pcm, h)[: len(ref_pcm) + len(h)] * gain
    if clip:
        echo = np.tanh(clip * echo) / clip                      # a loud speaker clips a little
    return echo.astype("float32")


def run(voices, *, delay=0.2, gain=0.25, reverb=0.15, clip=0.0, user_at=None, user_level=1.0, seed=0, total=7.0):
    """Stream the mic in odd-sized blocks; return the wall time (relative to ref start) at which talk-over was flagged, or None."""
    ref_pcm, user_pcm = voices
    rng = np.random.default_rng(seed)
    t_ref = 100.0                                                # the wall time we told the reference playback began
    ref = EchoReference(); ref.add(t_ref, ref_pcm)
    det = TalkOverDetector(ref)
    mic = np.zeros(int(total * SR), dtype="float32")
    echo = room(ref_pcm, delay, gain, reverb, clip, seed)
    mic[: min(len(mic), len(echo))] += echo[: len(mic)]
    mic += rng.normal(0, 0.003, len(mic)).astype("float32")
    if user_at is not None:
        # "level" is relative to the echo's own peak loudness
        u = user_pcm / (np.abs(user_pcm).max() + 1e-9) * (np.abs(echo).max() * user_level)
        s = int(user_at * SR)
        mic[s: s + len(u)] += u[: len(mic) - s]
    pos, block = 0, 533
    while pos < len(mic):
        chunk = mic[pos: pos + block]
        pos += len(chunk)
        det.feed(chunk, t_ref + pos / SR)
        if det.talking_over():
            return pos / SR, det
    return None, det


@pytest.mark.parametrize("cfg", [dict(delay=0.15, gain=0.2, reverb=0.0), dict(delay=0.35, gain=0.5, reverb=0.25),
                                 dict(delay=0.55, gain=0.3, reverb=0.4), dict(delay=0.2, gain=0.9, reverb=0.2, clip=6.0)])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_its_own_voice_never_counts_as_you(voices, cfg, seed):
    when, det = run(voices, seed=seed, **cfg)
    assert when is None, f"false barge-in at {when:.2f}s with {cfg} (lag {det.lag}, gain {det.gain})"


@pytest.mark.parametrize("cfg", [dict(delay=0.15, gain=0.2, reverb=0.0), dict(delay=0.35, gain=0.5, reverb=0.25), dict(delay=0.2, gain=0.9, reverb=0.2, clip=6.0)])
@pytest.mark.parametrize("level", [1.5, 3.0])
def test_you_talking_over_it_is_noticed_quickly(voices, cfg, level):
    if level == 1.5 and cfg.get("clip"):
        pytest.xfail("known limit: a clipping speaker plus a voice only 1.5x its loudness is not separable by loudness alone (F10 still works)")
    start = 3.5
    when, det = run(voices, user_at=start, user_level=level, **cfg)
    assert when is not None, f"never noticed you at level {level} with {cfg}"
    limit = 1.0 if level >= 3.0 else 2.0                # a faint voice over a clipping speaker takes longer; a clear one must be quick
    assert when - start < limit, f"noticed you only after {when - start:.2f}s"
    assert abs(det.lag - cfg["delay"]) < 0.12            # it really learned the delay


def test_learns_the_delay_and_gain(voices):
    ref = voices[0]
    echo = room(ref, 0.3, 0.4, 0.1)
    true_gain = float(np.sqrt(np.mean(echo[: len(ref)] ** 2)) / np.sqrt(np.mean(ref ** 2)))      # what the room really does
    _, det = run(voices, delay=0.3, gain=0.4, reverb=0.1)
    assert abs(det.lag - 0.3) < 0.1
    assert 0.6 * true_gain < det.gain < 1.5 * true_gain, (det.gain, true_gain)


def test_calibration_carries_over_so_the_next_sentence_is_protected_from_the_start(voices):
    _, det = run(voices, delay=0.3, gain=0.4)
    lag, gain = det.lag, det.gain
    det.reset()                                          # next utterance
    assert det.calibrated and det.lag == lag and det.gain == gain


def test_silence_between_our_sentences_does_not_trigger(voices):
    ref_pcm, _ = voices
    gap = np.zeros(int(1.2 * SR), dtype="float32")
    two = np.concatenate([ref_pcm[: int(3 * SR)], gap, ref_pcm[int(3 * SR): int(6 * SR)]])
    when, det = run((two, voices[1]), delay=0.25, gain=0.3, reverb=0.3, total=8.0)
    assert when is None
