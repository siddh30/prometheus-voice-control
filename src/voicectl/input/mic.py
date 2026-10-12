from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd


class Mic:
    """Records while started (F9 dictation and F10 conversations both press to start and press to stop); closed otherwise.
    `on_level` gets an RMS level (0..1) per block, called from the audio thread."""

    _open_streams = 0                # streams open in this process (any Mic): PortAudio may only be refreshed when none are

    def __init__(self, sample_rate: int = 16000, on_level: Callable[[float], None] | None = None) -> None:
        self.sample_rate = sample_rate
        self.on_level = on_level
        self._lock = threading.Lock()
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        with self._lock:
            self._frames.append(block)
        if self.on_level:
            try:
                self.on_level(min(1.0, float(np.sqrt(np.mean(block**2))) * 8))
            except Exception:
                pass    # the level meter is cosmetic; never let it abort the recording

    def start(self) -> None:
        with self._lock:
            self._frames = []
        self._open()

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
