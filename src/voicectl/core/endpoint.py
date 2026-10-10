from __future__ import annotations

from collections import deque

import numpy as np


class Endpointer:
    """Energy-based end-of-utterance detector for hands-free listening.

    The speech threshold adapts continuously: it follows the *quiet* part (10th percentile) of the last ~2 s of
    audio, times 3.5, clamped to [min_threshold, max_threshold]. So a loud start (speaker echo, a cough, you
    already talking) can't leave the threshold stuck above normal speech, and a humming room is still ignored.
    The first `warmup_s` are ignored entirely (tail of the assistant's own voice).

    feed() returns:
      "continue" - keep listening
      "done"     - speech happened and was followed by `silence_s` of quiet
      "timeout"  - nobody spoke for `no_speech_s`, or the utterance hit `max_s`
    """

    def __init__(self, sample_rate: int = 16000, silence_s: float = 1.0, min_speech_s: float = 0.25,
                 no_speech_s: float = 15.0, max_s: float = 30.0, warmup_s: float = 0.25,
                 min_threshold: float = 0.012, max_threshold: float = 0.05, history_s: float = 2.0,
                 threshold: float | None = None) -> None:
        self.sr = sample_rate
        self.silence_s, self.min_speech_s, self.no_speech_s, self.max_s = silence_s, min_speech_s, no_speech_s, max_s
        self.warmup_s, self.min_threshold, self.max_threshold = warmup_s, min_threshold, max_threshold
        self._fixed = threshold                    # caller-supplied (always-on listening keeps its own)
        self._hist: deque[float] = deque(maxlen=max(5, int(history_s * sample_rate / (sample_rate // 30))))
        self._threshold = min(threshold, max_threshold) if threshold else min_threshold
        self._t = 0.0
        self._speech_s = 0.0
        self._quiet_s = 0.0
        self.heard_speech = False
        self.peak = 0.0                             # loudest block seen (for diagnostics)

    @property
    def threshold(self) -> float:
        return self._threshold

    def feed(self, block: np.ndarray) -> str:
        dur = len(block) / self.sr
        self._t += dur
        rms = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
        self.peak = max(self.peak, rms)
        self._hist.append(rms)

        if self._fixed is None and len(self._hist) >= 5:
            floor = float(np.percentile(self._hist, 10))
            self._threshold = min(self.max_threshold, max(self.min_threshold, floor * 3.5))

        if self._t > self.warmup_s:
            if rms > self._threshold:
                self._speech_s += dur
                self._quiet_s = 0.0
                if self._speech_s >= self.min_speech_s:
                    self.heard_speech = True
            else:
                self._quiet_s += dur

        if self.heard_speech and self._quiet_s >= self.silence_s:
            return "done"
        if not self.heard_speech and self._t >= self.no_speech_s:
            return "timeout"
        if self._t >= self.max_s:
            return "done" if self.heard_speech else "timeout"
        return "continue"
