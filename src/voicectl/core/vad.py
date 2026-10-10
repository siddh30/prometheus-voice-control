from __future__ import annotations

import numpy as np


class VadEndpointer:
    """End-of-turn detection with a neural voice-activity model (Silero, bundled with faster-whisper).

    Unlike an energy threshold it ignores clicks, hum, fans and speaker echo, so "you stopped talking" is
    decided by speech, not by volume. Call update() every ~0.2-0.3 s with the audio so far; it only re-analyses
    the trailing `window_s` seconds (cheap) and remembers where speech last ended.

      "continue" - keep listening
      "done"     - speech was heard and `silence_s` has passed since it ended
      "timeout"  - nobody spoke within `no_speech_s`, or the turn hit `max_s`
    """

    def __init__(self, sample_rate: int = 16000, silence_s: float = 1.0, no_speech_s: float = 15.0,
                 max_s: float = 45.0, window_s: float = 8.0, min_speech_s: float = 0.25, base_sample: int = 0) -> None:
        from faster_whisper.vad import VadOptions
        self.sr = sample_rate
        self.silence_s, self.no_speech_s, self.max_s, self.window_s = silence_s, no_speech_s, max_s, window_s
        self.opts = VadOptions(threshold=0.5, min_speech_duration_ms=int(min_speech_s * 1000),
                               min_silence_duration_ms=300, speech_pad_ms=100)
        self.base = base_sample           # this endpointer only cares about audio from here on
        self.heard_speech = False
        self.first_speech_start: int | None = None     # absolute sample index
        self.last_speech_end = 0          # absolute sample index
        self.speech_s = 0.0

    def update(self, tail: np.ndarray, total_samples: int, gate: float | None = None) -> str:
        """`tail` = the most recent audio (at least `window_s` worth if available); `total_samples` = all audio so far.
        `gate`: only count speech segments at least this loud (RMS)."""
        from faster_whisper.vad import get_speech_timestamps
        offset = total_samples - len(tail)
        for seg in get_speech_timestamps(tail, self.opts, sampling_rate=self.sr):
            if gate is not None:         # echo of our own voice is speech-like but quieter than someone talking over it
                part = tail[seg["start"]:seg["end"]]
                if len(part) == 0 or float(np.sqrt(np.mean(part ** 2))) < gate:
                    continue
            self.heard_speech = True
            start = offset + seg["start"]
            self.first_speech_start = start if self.first_speech_start is None else min(self.first_speech_start, start)
            self.last_speech_end = max(self.last_speech_end, offset + seg["end"])
        elapsed = (total_samples - self.base) / self.sr
        if self.heard_speech and (total_samples - self.last_speech_end) / self.sr >= self.silence_s:
            return "done"
        if not self.heard_speech and elapsed >= self.no_speech_s:
            return "timeout"
        if elapsed >= self.max_s:
            return "done" if self.heard_speech else "timeout"
        return "continue"
