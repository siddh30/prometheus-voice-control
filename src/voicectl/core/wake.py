from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Callable

import numpy as np

from voicectl.core.config import Config
from voicectl.core.endpoint import Endpointer
from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.wake")

LEADERS = {"hey", "hi", "hello", "okay", "ok", "yo", "oh"}


def _skeleton(w: str) -> str:
    """How a word sounds, roughly: 'prometheus' / 'promethius' / 'pro methias' all become 'prmts'."""
    w = re.sub(r"[^a-z]", "", w.lower())
    w = w.replace("th", "t").replace("ph", "f").replace("ck", "k").replace("c", "k").replace("z", "s").replace("x", "ks")
    w = w[:1] + re.sub(r"[aeiouyhw]", "", w[1:])
    return re.sub(r"(.)\1+", r"\1", w)


def _score(heard: str, word: str) -> float:
    """0..1: how much `heard` looks (spelling) or sounds (skeleton) like the wake word."""
    if len(heard) < 5:
        return 0.0
    spelled = difflib.SequenceMatcher(None, heard, word).ratio()
    sounds = difflib.SequenceMatcher(None, _skeleton(heard), _skeleton(word)).ratio()
    return max(spelled, sounds if sounds >= 0.9 else 0.0)


def find_wake(text: str, word: str = "prometheus", threshold: float = 0.75) -> tuple[bool, str]:
    """Is the wake word in `text`? Speech-to-text mangles names ("Promethius", "pro me the us", "from the use"), so match by
    spelling AND by sound, also across words split in two to four. Only counts near the start of the utterance, so a sentence
    that merely mentions the name doesn't wake it. Returns (found, rest-of-utterance)."""
    raw = text.split()
    words = [re.sub(r"[^\w']", "", t.lower()) for t in raw]
    for i in range(min(len(words), 5)):
        if i > 0 and not set(words[:i]) <= LEADERS:
            break
        best = max(((_score("".join(words[i:i + n]).removesuffix("'s"), word), -n) for n in (1, 2, 3, 4) if len(words[i:i + n]) == n),
                   default=(0.0, 0))
        if best[0] >= threshold:                                 # the best-scoring span; on a tie the shortest
            return True, re.sub(r"^\W+", "", " ".join(raw[i - best[1]:]))
    return False, ""


class WakeListener:
    """Always-on (while idle) local listener: detects speech segments, transcribes them with a small local
    model, and fires `on_wake(rest_of_utterance)` when the utterance starts with the wake word.
    Audio is never sent anywhere or kept; it is dropped after each check. Releases the mic whenever
    another mode is active."""

    def __init__(self, config: Config, bus: EventBus, mic, stt, is_idle: Callable[[], bool],
                 on_wake: Callable[[str, str], None]) -> None:
        self.config, self.bus, self.mic, self.stt = config, bus, mic, stt
        self.is_idle, self.on_wake = is_idle, on_wake
        self.word = config.get("wake_word.word", "prometheus").lower()
        self.enabled = bool(config.get("wake_word.enabled", True))
        self._threshold: float | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self.bus.publish("wake", listening=on)

    async def _loop(self) -> None:
        recording = False
        try:
            while True:
                if not (self.enabled and self.is_idle()):
                    if recording:
                        self.mic.stop()
                        recording = False
                    await asyncio.sleep(0.3)
                    continue
                if not recording:
                    self.mic.start()
                    recording = True
                    self.bus.publish("wake", listening=True)
                audio = await self._next_utterance()
                if audio is None or not (self.enabled and self.is_idle()):
                    continue
                text = await asyncio.to_thread(self.stt.transcribe, audio, self.mic.sample_rate, self.word.title())
                found, rest = find_wake(text, self.word)
                log.debug("wake check: %r -> %s", text, found)
                if found:
                    self.mic.stop()          # hand the mic over before the assistant starts recording
                    recording = False
                    self.on_wake(rest, text)
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            if recording:
                self.mic.stop()

    async def _next_utterance(self) -> np.ndarray | None:
        """Block until one short spoken utterance has been captured (or ~3 s of quiet passes)."""
        ep = Endpointer(self.mic.sample_rate, silence_s=0.6, min_speech_s=0.2, no_speech_s=3.0, max_s=6.0,
                        threshold=self._threshold)
        idx, verdict, blocks_all = 0, "continue", []
        while verdict == "continue":
            await asyncio.sleep(0.05)
            if not (self.enabled and self.is_idle()):
                return None
            new, idx = self.mic.blocks_since(idx)
            for b in new:
                blocks_all.append(b)
                verdict = ep.feed(b)
                if verdict != "continue":
                    break
        if self._threshold is None:
            self._threshold = ep.threshold            # keep the calibrated noise floor for later windows
        # drop the buffered audio so memory doesn't grow; start a fresh stream window
        self.mic.stop(); self.mic.start()
        if verdict == "done" and ep.heard_speech:
            return np.concatenate(blocks_all)
        return None
