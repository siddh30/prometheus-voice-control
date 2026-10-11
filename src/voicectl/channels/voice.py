from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable

from voicectl.core.audio_output import wearing_headphones
from voicectl.core.ears import Ears
from voicectl.core.echo import TalkOverDetector
from voicectl.core.events import EventBus
from voicectl.core.vad import VadEndpointer

log = logging.getLogger("voicectl.voice")

STEP_DOWN = {"full": "echo", "echo": "half"}


class VoiceChannel:
    """Everything about HEARING you: the always-open mic stream (ears), whether the speakers' echo can be told apart
    from you (duplex: full | echo | half), catching our own voice, and the health of the microphone.
    It knows nothing about turns, agents or the widget layout; the conversation gives it callbacks."""

    def __init__(self, config, bus: EventBus, speaker, *, on_speech_start: Callable[[], None],
                 quiet: Callable[[], bool] = lambda: False) -> None:
        self.config, self.bus, self.speaker = config, bus, speaker
        self.on_speech_start = on_speech_start
        self.quiet = quiet                          # True while the mic doesn't matter (you are typing): no mic warnings
        self.duplex = "half"                        # full (headphones) | echo (speakers, echo-aware) | half (speakers, deaf while it speaks)
        self.detector: TalkOverDetector | None = None
        self.ears: Ears | None = None
        self.self_heard = 0                         # times the mic caught our own voice this conversation
        self.false_barges = 0                       # times we cut ourselves off for nothing
        self.make_ears = self._default_ears         # tests substitute a scripted listener

    # ---- the listener ----
    def open(self, mic, transcribe, *, paused: bool = False) -> Ears:
        self.ears = self.make_ears(mic, transcribe)
        self.ears.paused = paused                   # typing mode: don't even open the microphone
        return self.ears

    def pause(self, on: bool) -> None:
        if self.ears:
            self.ears.paused = on

    def _default_ears(self, mic, transcribe=None) -> Ears:
        cfg, sr, speaker = self.config, mic.sample_rate, self.speaker

        def vad_factory(base: int) -> VadEndpointer:
            return VadEndpointer(sr, silence_s=cfg.get("conversation.silence_s", 0.6), no_speech_s=1e9,
                                 max_s=cfg.get("conversation.max_utterance_s", 45.0), base_sample=base)
        return Ears(mic, vad_factory, (lambda: speaker.active) if speaker else (lambda: False),
                    min_gate=cfg.get("conversation.barge_min_level", 0.04), on_speech_start=self.on_speech_start,
                    transcribe=transcribe, tentative_s=cfg.get("conversation.tentative_s", 0.3), deaf=self.deaf,
                    talk_over=self.detector, on_mic_problem=self.on_mic_problem, on_false_barge=self.on_false_barge)

    def deaf(self) -> bool:
        """When the mic must be thrown away: on speakers, while we speak (half) or just after we stop (echo tail)."""
        spk = self.speaker
        if spk is None or self.duplex == "full":
            return False
        guard = spk.quiet_for() < self.config.get("conversation.echo_guard_s", 0.8)
        if self.duplex == "half":
            return spk.active or guard
        return (not spk.active) and guard                # echo mode listens WHILE we speak; only the fading tail is ignored

    # ---- headphones or speakers? ----
    async def decide_duplex(self) -> None:
        cfg = self.config
        output = cfg.get("conversation.output", "auto")        # auto | headphones | speakers
        if output == "auto":
            output = "headphones" if await asyncio.to_thread(wearing_headphones) else "speakers"
        if output == "headphones":
            self.duplex = "full"                                # no echo: talk over it freely
        elif cfg.get("conversation.barge_in", True) in (False, "false", "off"):
            self.duplex = "half"                                # speakers, voice interruption off: F10 only
        else:
            self.duplex = "echo"                                # speakers: predict our own echo and hear you on top of it
        reference = getattr(self.speaker, "reference", None)
        if self.duplex == "echo" and reference is None:          # no way to know what we play: fall back to the safe style
            self.duplex = "half"
        self.detector = TalkOverDetector(reference) if self.duplex == "echo" else None
        log.info("audio: %s", {"full": "headphones, talking over it is free", "echo": "speakers, echo-aware (it hears you over its own voice)",
                               "half": "speakers, deaf while it speaks (F10 interrupts)"}[self.duplex])
        self.bus.publish("duplex", mode=self.duplex)

    # ---- did we just hear ourselves? ----
    @staticmethod
    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9']+", text.lower())

    def is_echo(self, text: str) -> bool:
        """Did the mic just catch our own voice? Compare what we heard with what we recently said."""
        spoken = list(getattr(self.speaker, "spoken", []) or [])
        if not spoken:
            return False
        now, words = time.monotonic(), self.words(text)
        if not words:
            return True
        recent = [(ts, t) for ts, t in spoken if now - ts < 90]
        if len(words) <= 2:                                          # too short to judge: only if it is exactly our last words, just now
            last_ts, last_text = recent[-1] if recent else (0.0, "")
            return now - last_ts < 3 and all(w in self.words(last_text) for w in words)
        pool = {w for _, t in recent for w in self.words(t)}
        return sum(w in pool for w in words) / len(words) >= 0.8

    # ---- self-diagnosis: the microphone is a shared, fragile thing; adapt on evidence instead of assuming ----
    def step_down(self, why: str) -> None:
        """We keep hearing ourselves, so the assumed setup is wrong. Move to the next safer listening style and say so."""
        new = STEP_DOWN.get(self.duplex)
        if new == "echo" and getattr(self.speaker, "reference", None) is None:
            new = "half"
        if new is None:
            return
        old, self.duplex = self.duplex, new
        reference = getattr(self.speaker, "reference", None)
        self.detector = TalkOverDetector(reference) if new == "echo" and reference is not None else None
        if self.ears:
            self.ears.talk_over = self.detector
        self.self_heard = self.false_barges = 0
        label = {"echo": "listening while I speak, but ignoring my own voice", "half": "not listening while I speak (press F10 to interrupt)"}[new]
        log.warning("audio: %s -> %s (%s)", old, new, why)
        self.bus.publish("duplex", mode=new)
        self.bus.publish("notice", text=f"I kept hearing myself, so I'm now {label}.")

    def note_self_heard(self) -> None:
        self.self_heard += 1
        if self.self_heard >= 2:
            self.step_down("the mic caught our own voice twice")

    def on_false_barge(self) -> None:
        self.false_barges += 1
        if self.false_barges >= 2 and self.duplex == "echo":
            self.step_down("two false interruptions")

    def on_mic_problem(self, kind: str) -> None:
        texts = {"restarted": "The microphone changed, so I reconnected to it.",
                 "silent": "I'm getting no sound from the microphone. Check the input device and Microphone permission.",
                 "unavailable": "I can't reach the microphone right now. Check that it's connected."}
        log.warning("microphone: %s", kind)
        if kind == "silent" and self.quiet():
            return                                      # you are typing to it, so a quiet mic is not a problem worth a warning
        self.bus.publish("notice" if kind != "unavailable" else "error", **({"text": texts[kind]} if kind != "unavailable" else {"message": texts[kind]}))
