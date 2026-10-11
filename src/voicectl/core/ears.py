from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("voicectl.ears")


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0


@dataclass
class Utterance:
    audio: np.ndarray
    text_task: asyncio.Task | None = None      # transcription, possibly already finished (started speculatively)
    speech_end_at: float = 0.0                 # monotonic time you stopped talking (for latency logging)
    engaged: bool = False                      # you talked OVER the assistant (a false one is how we notice echo problems)


class Ears:
    """The hearing half of a conversation. One mic stream that never closes: it continuously cuts the audio into
    utterances (neural VAD) and queues each finished one for transcription, while the assistant is thinking, working or
    speaking. `on_speech_start` fires the instant you begin talking, so the other half can stop at once.

    While the assistant's own voice is playing, only speech clearly louder than its echo counts."""

    def __init__(self, mic, vad_factory: Callable[[int], object], speaker_active: Callable[[], bool], *,
                 min_gate: float = 0.04, on_speech_start: Callable[[], None] = lambda: None,
                 poll_s: float = 0.08, preroll_s: float = 0.3, tail_s: float = 0.25, idle_trim_s: float = 10.0,
                 transcribe: Callable[[np.ndarray], Awaitable[str]] | None = None, tentative_s: float = 0.3,
                 deaf: Callable[[], bool] = lambda: False, talk_over=None,
                 on_mic_problem: Callable[[str], None] = lambda kind: None,
                 on_false_barge: Callable[[], None] = lambda: None, clock: Callable[[], float] = time.monotonic) -> None:
        self.mic, self.vad_factory, self.speaker_active = mic, vad_factory, speaker_active
        self.min_gate, self.on_speech_start = min_gate, on_speech_start
        self.poll_s, self.preroll_s, self.tail_s, self.idle_trim_s = poll_s, preroll_s, tail_s, idle_trim_s
        self.transcribe, self.tentative_s = transcribe, tentative_s
        self.on_mic_problem, self.on_false_barge = on_mic_problem, on_false_barge
        self.clock = clock                       # injectable, so tests never have to touch the global clock the event loop uses
        self.talk_over = talk_over               # TalkOverDetector: on speakers, hears you over our own voice (instead of going deaf)
        self.deaf = deaf                         # True while the assistant's own voice could reach the mic (speakers): hear nothing
        self.paused = False                      # True: the mic is released and nothing is heard (e.g. while music plays)
        self._queue: asyncio.Queue[Utterance | np.ndarray] = asyncio.Queue()

    async def next(self) -> Utterance | np.ndarray:
        """The next finished utterance (waits)."""
        return await self._queue.get()

    def _clip(self, vad, base: int, total: int) -> np.ndarray:
        sr = self.mic.sample_rate
        a = max(base, (vad.first_speech_start or base) - int(sr * self.preroll_s))
        b = min(total, vad.last_speech_end + int(sr * self.tail_s))
        return self.mic.slice(a, b)

    async def run(self) -> None:
        sr = self.mic.sample_rate
        window = int(sr * 8)
        base, vad = 0, self.vad_factory(0)
        started, echo = False, []
        engaged, engaged_at, fed, was_active = False, 0.0, 0, False
        restarts: deque[float] = deque(maxlen=3)
        silent_warned = silent_reopened = False     # engaged: you talked over us; fed: how much audio the echo detector has seen
        tentative: tuple[int, asyncio.Task] | None = None      # (speech end it was started for, transcription)
        self.mic.start()
        running = True
        try:
            while True:
                await asyncio.sleep(self.poll_s)
                if self.paused:
                    if running:
                        self.mic.stop()
                        running = False
                    continue
                if not running:                    # resumed: start clean, as if this were the first moment
                    self.mic.start()
                    running, base, vad, started, echo, tentative = True, 0, self.vad_factory(0), False, [], None
                    continue
                # Mic health: a vanished/changed device (Bluetooth switch, unplug) or a muted/denied input must not fail silently.
                if hasattr(self.mic, "stalled") and self.mic.stalled():
                    now = self.clock()
                    recent = [t for t in restarts if now - t < 60]
                    if len(recent) < 3:
                        restarts.append(now)
                        self.mic.restart()
                        self.on_mic_problem("restarted")
                    else:
                        self.on_mic_problem("unavailable")
                        await asyncio.sleep(2.0)
                    continue
                if hasattr(self.mic, "silent_for"):
                    quiet = self.mic.silent_for()
                    if quiet > 2.5 and not silent_reopened:
                        # A USB or Bluetooth microphone that starts returning pure silence usually comes back when it is opened again: do that
                        # at once instead of leaving "Listening" on screen with nothing arriving.
                        silent_reopened = True
                        now = self.clock()
                        if len([t for t in restarts if now - t < 60]) < 3:
                            restarts.append(now)
                            self.mic.restart()
                            self.on_mic_problem("restarted")
                    elif quiet > 6.0 and not silent_warned:
                        silent_warned = True
                        self.on_mic_problem("silent")
                    elif quiet < 1.0:
                        silent_warned = silent_reopened = False
                tail, total = self.mic.tail(window)
                active = self.speaker_active()
                if self.talk_over is not None and active and not engaged:
                    # Speakers: our own voice is in the mic. Don't run speech detection on it; ask the echo detector whether
                    # there is something ON TOP of the predicted echo (that is you).
                    snap_total, t_arrival = self.mic.snapshot()
                    fed = max(fed, snap_total - sr)                     # never replay old audio against the wrong timestamps
                    if snap_total > fed:
                        self.talk_over.feed(self.mic.slice(fed, snap_total), t_arrival)
                        fed = snap_total
                    was_active = True
                    if self.talk_over.talking_over():
                        engaged, engaged_at = True, self.clock()
                        base = max(base, total - int(sr * 0.5))        # start your utterance just before we noticed you
                        vad, started, tentative, echo = self.vad_factory(base), True, None, []
                        self.on_speech_start()                          # cuts the assistant off right now (once)
                    else:
                        base, vad, started, tentative, echo = total, self.vad_factory(total), False, None, []
                        self.mic.trim_before(total)
                        continue
                elif self.talk_over is not None and not active and was_active and not engaged:
                    self.talk_over.reset()                              # it stopped: keep what we learned about the room, drop the rest
                    was_active, fed = False, total
                if not engaged and self.deaf():     # half-duplex / the echo tail: throw away what the mic caught; start clean afterwards
                    if tentative:
                        tentative[1].cancel()
                    base, vad, started, tentative, echo = total, self.vad_factory(total), False, None, []
                    self.mic.trim_before(total)
                    continue
                fresh = total - base
                if fresh < sr * 0.2:
                    continue
                tail = tail[-min(len(tail), fresh):]            # only audio belonging to the current utterance
                gate = None
                if self.speaker_active() and self.talk_over is None:     # headphones/full duplex: loudness gate
                    if not vad.heard_speech:
                        echo = (echo + [rms(tail[-int(sr * 0.3):])])[-30:]
                    gate = max(self.min_gate, 2.2 * float(np.percentile(echo, 90))) if echo else self.min_gate
                verdict = await asyncio.to_thread(vad.update, tail, total, gate)

                if engaged and not vad.heard_speech and self.clock() - engaged_at > 2.5:
                    # we cut ourselves off but nobody spoke: it was a false alarm (echo, a cough). Carry on listening normally.
                    engaged, started = False, False
                    base, vad = total, self.vad_factory(total)
                    if self.talk_over is not None:
                        self.talk_over.reset()
                        was_active, fed = False, total
                    self.on_false_barge()
                    continue
                if vad.heard_speech and not started:
                    started = True
                    self.on_speech_start()
                if started and self.transcribe and verdict != "done" and (total - vad.last_speech_end) / sr >= self.tentative_s \
                        and (tentative is None or tentative[0] != vad.last_speech_end):
                    # Probably finished: start transcribing now, before the full end-of-speech wait elapses.
                    # If you carry on talking, last_speech_end moves and this result is simply not used.
                    if tentative:
                        tentative[1].cancel()
                    tentative = (vad.last_speech_end, asyncio.create_task(self.transcribe(self._clip(vad, base, total))))
                if started and verdict == "done":
                    clip = self._clip(vad, base, total)
                    task = None
                    if self.transcribe:
                        if tentative and tentative[0] == vad.last_speech_end:
                            task = tentative[1]                  # already running (or done) for exactly this audio
                        else:
                            if tentative:
                                tentative[1].cancel()
                            task = asyncio.create_task(self.transcribe(clip))
                    ended = time.monotonic() - (total - vad.last_speech_end) / sr
                    await self._queue.put(Utterance(clip, task, ended, engaged))
                    base, vad, started, tentative = total, self.vad_factory(total), False, None
                    engaged = False
                    if self.talk_over is not None:
                        self.talk_over.reset()
                        was_active, fed = False, total
                    self.mic.trim_before(base - sr)
                elif not started and (verdict == "timeout" or fresh > sr * self.idle_trim_s):
                    base = total - int(sr * 2)                   # long quiet: drop old audio, keep 2 s of lead-in
                    vad = self.vad_factory(base)
                    self.mic.trim_before(base)
        finally:
            self.mic.stop()
