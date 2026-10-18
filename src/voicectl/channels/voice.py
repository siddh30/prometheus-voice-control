from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.voice")


@dataclass
class Utterance:
    audio: np.ndarray
    speech_end_at: float = 0.0                 # monotonic time you stopped talking (for latency logging)


class PushRecorder:
    """Press once to start recording, press again to send. The microphone is open only in between, so there is nothing
    to listen for, nothing to echo-cancel and no wake word. Starting a recording cuts off whatever the assistant was
    saying or doing (`on_start`), so pressing the key is also how you interrupt it."""

    def __init__(self, mic, *, on_start: Callable[[], None] = lambda: None, on_discard: Callable[[], None] = lambda: None,
                 on_problem: Callable[[str], None] = lambda kind: None, min_s: float = 0.3, max_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.mic, self.on_start, self.on_discard, self.on_problem = mic, on_start, on_discard, on_problem
        self.min_s, self.max_s, self.clock = min_s, max_s, clock
        self.paused = False                       # typing mode: presses are ignored and the mic is never opened
        self.recording = False
        self._t0 = 0.0
        self._cap: asyncio.Task | None = None
        self._queue: asyncio.Queue[Utterance] = asyncio.Queue()

    async def next(self) -> Utterance:
        """The next finished recording (waits)."""
        return await self._queue.get()

    async def run(self) -> None:
        """Lives as long as the conversation; releases the microphone when it ends."""
        try:
            await asyncio.Event().wait()
        finally:
            self.cancel()

    def press(self) -> None:
        if self.paused:
            return
        self._finish() if self.recording else self._begin()

    def _begin(self) -> None:
        self.on_start()                           # stop talking / working first: our voice must not be in your recording
        try:
            self.mic.start()
        except Exception as e:                    # no usable microphone: say so instead of sitting in "listening" forever
            log.error("microphone failed to open: %s", e)
            self.on_problem("unavailable")
            self.on_discard()
            return
        self.recording, self._t0 = True, self.clock()
        self._cap = asyncio.get_running_loop().create_task(self._limit())

    async def _limit(self) -> None:
        await asyncio.sleep(self.max_s)           # a forgotten recording ends itself
        if self.recording:
            self._finish()

    def _finish(self) -> None:
        held = self.clock() - self._t0
        audio = self._release()
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if held < self.min_s or audio.size == 0:
            self.on_problem("too_short" if held < self.min_s else "no_audio")
            self.on_discard()
            return
        if peak < 0.005:
            self.on_problem("silent")             # recorded, but the mic returned silence: permission or input device
        self._queue.put_nowait(Utterance(audio, speech_end_at=self.clock()))

    def _release(self) -> np.ndarray:
        if self._cap:
            self._cap.cancel()
            self._cap = None
        self.recording = False
        try:
            return self.mic.stop()
        except Exception:
            return np.zeros(0, "float32")

    def cancel(self) -> None:
        """Drop a recording in progress without sending it (Escape, typing mode, the conversation ending)."""
        if self.recording:
            self._release()


class VoiceChannel:
    """Everything about HEARING you: push-to-talk recording and the health of the microphone.
    It knows nothing about turns, agents or the widget layout; the conversation gives it callbacks."""

    def __init__(self, config, bus: EventBus, speaker, *, on_start: Callable[[], None], on_discard: Callable[[], None] = lambda: None,
                 quiet: Callable[[], bool] = lambda: False) -> None:
        self.config, self.bus, self.speaker = config, bus, speaker
        self.on_start, self.on_discard = on_start, on_discard
        self.quiet = quiet                          # True while the mic doesn't matter (you are typing): no mic warnings
        self.recorder: PushRecorder | None = None
        self.make_recorder = self._default_recorder  # tests substitute a scripted recorder

    # ---- the recorder ----
    def open(self, mic, *, paused: bool = False) -> PushRecorder:
        self.recorder = self.make_recorder(mic)
        self.recorder.paused = paused               # typing mode: the microphone is never opened
        return self.recorder

    def press(self) -> None:
        """The key: start recording, or send what was recorded."""
        if self.recorder:
            self.recorder.press()

    def pause(self, on: bool) -> None:
        if self.recorder:
            self.recorder.paused = on
            if on:
                self.recorder.cancel()              # typing mode begins: drop a recording in progress

    @property
    def recording(self) -> bool:
        return bool(self.recorder and self.recorder.recording)

    def _default_recorder(self, mic) -> PushRecorder:
        cfg = self.config
        return PushRecorder(mic, on_start=self.on_start, on_discard=self.on_discard, on_problem=self.on_mic_problem,
                            min_s=cfg.get("recording.min_hold_ms", 300) / 1000, max_s=cfg.get("recording.max_hold_s", 120))

    # ---- the microphone is a shared, fragile thing: say what is wrong in plain words ----
    def on_mic_problem(self, kind: str) -> None:
        texts = {"silent": "I'm getting no sound from the microphone. Check the input device and Microphone permission.",
                 "unavailable": "I can't reach the microphone. Check that it's connected and that Microphone access is allowed.",
                 "too_short": "That was too short. Press Ctrl+F10, speak, then press Ctrl+F10 again.",
                 "no_audio": "The microphone returned no audio. Check the input device in System Settings > Sound."}
        log.warning("microphone: %s", kind)
        if kind in ("silent", "too_short") and self.quiet():
            return                                  # you are typing to it, so the microphone is not a problem worth a warning
        self.bus.publish("error" if kind == "unavailable" else "notice", **({"message": texts[kind]} if kind == "unavailable" else {"text": texts[kind]}))
