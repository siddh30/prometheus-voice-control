from __future__ import annotations

"""Was anyone actually speaking? Speech-to-text models invent words for silence ("Thank you.", "Thanks for watching"), and in dictation
whatever comes back is TYPED into the app in front, so audio with no speech in it must never reach the model."""

import numpy as np


def has_speech(audio: np.ndarray, sample_rate: int = 16000, min_speech_s: float = 0.15, floor: float = 0.010) -> bool:
    """True if there is at least `min_speech_s` of sound clearly above the room's background level and a real peak.
    Deliberately forgiving of quiet voices: it only rejects silence and steady background noise."""
    if audio is None or audio.size < sample_rate * min_speech_s:
        return False
    frame = max(1, int(sample_rate * 0.03))
    n = audio.size // frame
    if n == 0:
        return False
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    background = float(np.percentile(rms, 20))
    loud = rms > max(floor, background * 3.0)
    return bool(loud.sum() * 0.03 >= min_speech_s and float(np.abs(audio).max()) >= 0.02)
