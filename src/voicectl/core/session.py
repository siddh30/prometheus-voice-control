from __future__ import annotations

import logging
import asyncio
import functools
import time
from collections.abc import Callable
from enum import StrEnum

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import Mode
from voicectl.stt.base import STTBackend
from voicectl.telemetry import phoenix as tel


log = logging.getLogger("voicectl.session")


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    HANDLING = "handling"


class MicLike:
    sample_rate: int
    on_level: Callable[[float], None] | None
    def start(self) -> None: ...
    def stop(self) -> np.ndarray: ...


class SessionManager:
    """Push-to-talk state machine: key down -> record, key up -> transcribe -> dispatch to the mode."""

    def __init__(self, config: Config, bus: EventBus, mic: MicLike, stt: STTBackend,
                 modes: dict[str, Mode], speaker=None, convo_stt=None) -> None:
        self.speaker = speaker
        self.convo_stt = convo_stt           # smaller/faster model for hands-free conversation; dictation keeps `stt`
        self.config, self.bus, self.mic, self.stt, self.modes = config, bus, mic, stt, modes
        self.state = State.IDLE
        self.active_mode: str | None = None
        self._started_at = 0.0
        self._cap_task: asyncio.Task | None = None
        self._cap_s = config.get("recording.max_hold_s", 120)
        self._min_s = config.get("recording.min_hold_ms", 300) / 1000
        self._toggle = config.get("recording.mode", "toggle") == "toggle"
        self._loop = asyncio.get_running_loop()
        self._conv_task: asyncio.Task | None = None
        mic.on_level = self._on_level

    # --- hotkey callbacks (run on the event loop) ---
    def key_down(self, mode: str) -> None:
        mode_obj = self.modes.get(mode)
        if mode_obj is not None and mode_obj.continuous:
            self._toggle_continuous(mode, mode_obj)
            return
        if self._conv_task and not self._conv_task.done():
            # Never leave the user stuck: any mode key ends the conversation, then does what it normally does.
            self._conv_task.cancel()
            asyncio.get_running_loop().call_later(0.5, self.key_down, mode)
            return
        if self.state is State.LISTENING and self._toggle and mode == self.active_mode:
            asyncio.get_running_loop().create_task(self._finish())   # second press stops
            return
        if self.speaker:
            self.speaker.stop()        # talking over you would also pollute the recording
        if self.state is not State.IDLE or mode not in self.modes:
            return
        self.active_mode = mode
        self._started_at = time.monotonic()
        self._set(State.LISTENING)
        try:
            self.mic.start()
        except Exception as e:                    # no usable microphone: say so instead of sitting in "listening" forever
            log.error("microphone failed to open: %s", e)
            self.bus.publish("error", message="I can't reach a microphone. Check the input device in System Settings > Sound.")
            self._set(State.IDLE)
            return
        self._cap_task = asyncio.get_running_loop().create_task(self._cap(mode))

    def key_up(self, mode: str) -> None:
        if self._toggle:
            return
        if self.state is State.LISTENING and mode == self.active_mode:
            asyncio.get_running_loop().create_task(self._finish())

    def conversation_active(self) -> bool:
        return bool(self._conv_task and not self._conv_task.done())

    def wake(self, mode: str, initial: str = "", heard: str = "") -> None:
        """Start a hands-free conversation by voice (the wake word). No-op unless idle."""
        mode_obj = self.modes.get(mode)
        if mode_obj is None or self.state is not State.IDLE or (self._conv_task and not self._conv_task.done()):
            return
        if heard:                                   # show what you said ("Prometheus"), like any other utterance
            self.bus.publish("transcript", text=heard, mode=mode)
        self._start_continuous(mode, mode_obj, initial)

    def _toggle_continuous(self, mode: str, mode_obj: Mode) -> None:
        if self._conv_task and not self._conv_task.done():
            interrupt = getattr(mode_obj, "interrupt", None)
            if interrupt and interrupt():   # it was thinking/speaking: cut it off and keep the conversation going
                return
            self._conv_task.cancel()        # it was waiting for you: the press ends the conversation
            return
        if self.state is not State.IDLE:
            return
        self._start_continuous(mode, mode_obj)

    def _start_continuous(self, mode: str, mode_obj: Mode, initial: str = "") -> None:
        if self.speaker:
            self.speaker.stop()
        self.active_mode = mode
        self._set(State.HANDLING)
        self._conv_task = asyncio.get_running_loop().create_task(self._run_continuous(mode_obj, initial))

    async def _run_continuous(self, mode_obj: Mode, initial: str = "") -> None:
        with tel.tracer.start_as_current_span(f"voicectl.mode.{mode_obj.name}"):
            try:
                await mode_obj.run(self.mic, self.convo_stt or self.stt, initial)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.bus.publish("error", message=str(e))
            finally:
                self._set(State.IDLE)

    def cancel(self) -> None:
        if self._conv_task and not self._conv_task.done():
            self._conv_task.cancel()
            return
        if self.state is State.LISTENING:
            self._stop_recording()
            self._set(State.IDLE)
            self.bus.publish("notice", text="Cancelled")

    # --- internals ---
    def _on_level(self, level: float) -> None:
        # Called from the audio thread; asyncio queues are not thread-safe.
        self._loop.call_soon_threadsafe(functools.partial(self.bus.publish, "level", value=level))

    async def _cap(self, mode: str) -> None:
        await asyncio.sleep(self._cap_s)
        if self.state is State.LISTENING and self.active_mode == mode:
            await self._finish()

    def _stop_recording(self) -> np.ndarray:
        if self._cap_task:
            self._cap_task.cancel()
            self._cap_task = None
        return self.mic.stop()

    def _set(self, state: State) -> None:
        self.state = state
        self.bus.publish("state", state=state.value, mode=self.active_mode)

    async def _finish(self) -> None:
        if self.state is not State.LISTENING:
            return
        held = time.monotonic() - self._started_at
        audio = self._stop_recording()
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if held < self._min_s or audio.size == 0:
            reason = "too short" if held < self._min_s else "mic returned no audio"
            self.bus.publish("notice", text=f"Ignored: {reason} (held {held * 1000:.0f} ms, "
                                            f"{audio.size} samples, peak {peak:.3f})")
            self._set(State.IDLE)
            return
        if peak < 0.005:
            self.bus.publish("notice", text=f"Audio is silent (peak {peak:.4f}): check mic permission/input device")
        mode = self.modes[self.active_mode]
        try:
            with tel.tracer.start_as_current_span("voicectl.session") as span:
                span.set_attribute("voicectl.mode", self.active_mode)
                span.set_attribute("voicectl.audio_seconds", round(audio.size / self.mic.sample_rate, 2))
                span.set_attribute("voicectl.held_ms", round(held * 1000))
                try:
                    self._set(State.TRANSCRIBING)
                    with tel.tracer.start_as_current_span("voicectl.stt") as s:
                        text = await asyncio.to_thread(self.stt.transcribe, audio, self.mic.sample_rate)
                        s.set_attribute("voicectl.transcript", text)
                        s.set_attribute("voicectl.stt_backend", type(self.stt).__name__)
                    if not text:
                        self.bus.publish("notice", text="Didn't catch that")
                        return
                    span.set_attribute("input.value", text)
                    self.bus.publish("transcript", text=text, mode=self.active_mode)
                    self._set(State.HANDLING)
                    with tel.tracer.start_as_current_span(f"voicectl.mode.{self.active_mode}"):
                        await mode.handle(text)
                except Exception as e:  # keep the daemon alive; surface the failure to the UI
                    span.record_exception(e)
                    self.bus.publish("error", message=str(e))
        finally:
            self._set(State.IDLE)
