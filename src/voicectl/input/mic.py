from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np
import sounddevice as sd


class Mic:
    """Records while started. `on_level` gets an RMS level (0..1) per block, called from the audio thread.

    Sample indices are absolute (since start()), even after old audio is trimmed away, so a long-running
    listener can keep asking about "samples 160000..192000" without the buffer growing forever."""

    def __init__(self, sample_rate: int = 16000, on_level: Callable[[float], None] | None = None) -> None:
        self.sample_rate = sample_rate
        self.on_level = on_level
        self._lock = threading.Lock()
        self._frames: list[np.ndarray] = []
        self._dropped = 0            # samples trimmed from the front
        self._total = 0              # samples recorded since start(), including trimmed ones
        self._stream: sd.InputStream | None = None
        self._stamp = (0, 0.0)       # (total samples, time.monotonic()) when the latest block arrived
        self._clock = time.monotonic
        self._last_sound = 0.0       # last time a block carried any signal at all
        self._started_at = 0.0

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        with self._lock:
            self._frames.append(block)
            self._total += len(block)
            now = self._clock()
            self._stamp = (self._total, now)
            if float(np.abs(block).max()) > 1e-4:
                self._last_sound = now
        if self.on_level:
            try:
                self.on_level(min(1.0, float(np.sqrt(np.mean(block**2))) * 8))
            except Exception:
                pass    # the level meter is cosmetic; never let it abort the recording

    def blocks_since(self, index: int) -> tuple[list[np.ndarray], int]:
        """New audio blocks recorded after `index` and the next index to ask from (energy endpointing / wake word)."""
        frames = self._frames
        return frames[index:], len(frames)

    def blocks_from(self, sample_index: int) -> tuple[list[np.ndarray], int]:
        """Audio recorded since absolute sample `sample_index`, and the next index to ask from. Unlike blocks_since() this stays correct
        while old audio is trimmed, so a listener can run for hours on one open stream with no gap."""
        with self._lock:
            out, pos = [], self._dropped
            for b in self._frames:
                end = pos + len(b)
                if end > sample_index:
                    out.append(b if pos >= sample_index else b[sample_index - pos:])
                pos = end
            return out, pos

    @property
    def total_samples(self) -> int:
        return self._total

    def stalled(self, timeout: float = 1.5) -> bool:
        """The stream is open but no audio has arrived for `timeout` s: the device vanished or changed (Bluetooth switch, unplug)."""
        last = self._stamp[1] or self._started_at                      # a stream that NEVER delivers a block counts too
        return self._stream is not None and self._clock() - last > timeout

    def silent_for(self) -> float:
        """Seconds of pure digital silence (all zeros): typical of a muted input or a revoked Microphone permission."""
        if self._stream is None or self._stamp[1] == 0:
            return 0.0
        return max(0.0, self._stamp[1] - self._last_sound) if self._last_sound else self._clock() - self._started_at

    def restart(self) -> None:
        """Re-open the (possibly different) default input device."""
        try:
            if self._stream:
                self._stream.close()
        except Exception:
            pass
        if self._stream is not None:
            Mic._open_streams = max(0, Mic._open_streams - 1)
        self._stream = None
        self._reopen()

    def snapshot(self) -> tuple[int, float]:
        """(total samples, wall time they were captured): ties sample positions to the clock, for echo alignment."""
        with self._lock:
            return self._stamp

    def tail(self, n_samples: int) -> tuple[np.ndarray, int]:
        """The most recent `n_samples` of audio and the absolute total recorded so far."""
        with self._lock:
            frames, total = list(self._frames), self._total
        out, have = [], 0
        for block in reversed(frames):
            out.append(block)
            have += len(block)
            if have >= n_samples:
                break
        audio = np.concatenate(out[::-1]) if out else np.zeros(0, dtype="float32")
        return audio[-n_samples:], total

    def slice(self, start: int, end: int) -> np.ndarray:
        """Audio between absolute sample indices [start, end) (clamped to what is still buffered)."""
        with self._lock:
            frames, pos = list(self._frames), self._dropped
        parts = []
        for block in frames:
            b0, b1 = pos, pos + len(block)
            if b1 > start and b0 < end:
                parts.append(block[max(0, start - b0): min(len(block), end - b0)])
            pos = b1
        return np.concatenate(parts) if parts else np.zeros(0, dtype="float32")

    def trim_before(self, index: int) -> None:
        """Forget audio before absolute sample `index` (whole blocks only)."""
        with self._lock:
            while self._frames and self._dropped + len(self._frames[0]) <= index:
                self._dropped += len(self._frames.pop(0))

    def start(self) -> None:
        with self._lock:
            self._frames, self._dropped, self._total = [], 0, 0
        self._started_at = self._clock()
        self._last_sound = 0.0
        self._stamp = (0, 0.0)
        self._open()

    def _reopen(self) -> None:
        """Open a fresh stream but keep the audio buffer and the sample numbering (positions stay valid)."""
        self._started_at = self._clock()
        self._last_sound = 0.0
        self._stamp = (self._total, 0.0)
        self._open()

    _open_streams = 0                # streams open in this process (any Mic): PortAudio may only be refreshed when none are

    def _open(self) -> None:
        """Open the default input. Survives a device change (AirPods off, USB mic in): PortAudio caches the device list at start-up, so a
        stale list makes the open fail ("Audio Unit: Invalid Property Value"); refresh it and retry, and if the new device refuses our
        sample rate, record at its own rate and convert."""
        try:
            self._open_at(self.sample_rate)
            return
        except sd.PortAudioError:
            pass
        if Mic._open_streams == 0:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        try:
            self._open_at(self.sample_rate)
            return
        except sd.PortAudioError:
            pass
        native = int(sd.query_devices(kind="input")["default_samplerate"])         # raises if there is truly no input device
        self._open_at(native)

    def _open_at(self, rate: int) -> None:
        cb = self._callback if rate == self.sample_rate else self._resampler(rate)
        stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate / 30), callback=cb)
        stream.start()
        self._stream = stream
        Mic._open_streams += 1

    def _resampler(self, rate: int):
        ratio = rate / self.sample_rate

        def convert(indata, frames, time_info, status) -> None:
            x = indata[:, 0]
            if ratio > 1:                                                       # average over the window: a cheap low-pass before decimating
                k = max(1, int(round(ratio)))
                x = np.convolve(x, np.ones(k, dtype="float32") / k, mode="same")
            n = max(1, int(len(x) / ratio))
            out = np.interp(np.arange(n) * ratio, np.arange(len(x)), x).astype("float32")
            self._callback(out[:, None], n, time_info, status)
        return convert

    def stop(self) -> np.ndarray:
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
                Mic._open_streams = max(0, Mic._open_streams - 1)
        with self._lock:
            frames = list(self._frames)
        return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")
