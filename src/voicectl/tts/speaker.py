from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import subprocess
import tempfile
import time
import wave
from collections import deque

import numpy as np
from collections.abc import AsyncIterator
from pathlib import Path

from voicectl.core.config import DATA_DIR, Config
from voicectl.core.echo import EchoReference
from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.tts")


EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F900-\U0001F9FF\u200d\ufe0f\u2190-\u21FF]+")


def strip_emoji(text: str) -> str:
    """Emoji are read aloud as words ("musical note", "smiling face"): remove them from speech and from the captions."""
    return EMOJI.sub("", text)


def clean_for_speech(text: str, max_chars: int | None = 450) -> str:
    """Strip markdown/URLs and (optionally) trim to whole sentences, so answers are listenable.
    The full text stays on screen."""
    t = strip_emoji(text)
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)          # [label](url) -> label
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    if max_chars is None or len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…").strip()


async def one(text: str) -> AsyncIterator[str]:
    yield text


def decode_pcm(path: Path) -> np.ndarray | None:
    """What we are about to play, as 16 kHz mono floats, so the echo detector can predict how it will sound in the mic."""
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "ref.wav"
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(out)],
                           check=True, capture_output=True, timeout=10)
            with wave.open(str(out), "rb") as w:
                return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768.0
    except Exception:
        return None


class Speaker:
    """Reads text aloud. Edge neural voices (free, online) with the offline macOS `say` as fallback.

    Text is synthesized to a file one chunk at a time while the previous chunk plays, so streamed
    replies start talking after the first sentence. speak()/speak_many() cancel whatever is playing;
    stop() is called when you start recording."""

    def __init__(self, config: Config, bus: EventBus, prefs_path: Path | None = None) -> None:
        self.config, self.bus = config, bus
        self.prefs_path = prefs_path or DATA_DIR / "prefs.json"
        self.voice_on = self._load_voice_pref()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self.active_since_first = False
        self.reference = EchoReference()                              # the loudness envelope of what we play (for hearing you over it)
        self.spoken: deque[tuple[float, str]] = deque(maxlen=40)     # (when, text) of what we said recently: the echo guard compares against it

    @property
    def active(self) -> bool:
        return bool(self._task and not self._task.done())

    @staticmethod
    def quiet_for() -> float:
        """Seconds since audio last finished playing (negative while a wake-up clip is still queued)."""
        return time.monotonic() - Speaker._last_end

    # ---- voice on/off (text-only mode): remembered across launches ----
    def _load_voice_pref(self) -> bool:
        default = bool(self.config.get("tts.enabled", True))
        try:
            return bool(json.loads(self.prefs_path.read_text()).get("voice", default))
        except (OSError, ValueError):
            return default

    def set_voice(self, on: bool) -> None:
        """Speak replies aloud (True) or show them as text only (False). With no audio there is also nothing for the mic to hear."""
        self.voice_on = on
        try:
            prefs = json.loads(self.prefs_path.read_text()) if self.prefs_path.exists() else {}
            prefs["voice"] = on
            self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self.prefs_path.write_text(json.dumps(prefs))
        except (OSError, ValueError):
            pass
        if not on:
            self.stop()                                          # cut off anything currently being said
        self.bus.publish("voice", on=on)

    def enabled_for(self, mode: str) -> bool:
        return self.voice_on and bool(self.config.get("tts.enabled", True) and self.config.get(f"tts.modes.{mode}", False))

    async def speak(self, text: str) -> None:
        await self.speak_many(one(clean_for_speech(text, self.config.get("tts.max_chars", 450))), clean=False)

    async def speak_many(self, chunks: AsyncIterator[str], clean: bool = True, show: bool = False) -> None:
        """Speak chunks in order; returns when finished or stopped. Safe to call from any task.
        show=True also publishes `speech_text` events that reveal the words in step with the voice."""
        if not self.voice_on:                                  # text only: pass the words through to the captions, make no sound
            shown = ""
            async for chunk in chunks:
                chunk = clean_for_speech(chunk, None) if clean else chunk
                if chunk:
                    shown = f"{shown} {chunk}".strip()
                    if show:
                        self.bus.publish("speech_text", text=shown)
            return
        self.stop()
        task = self._task = asyncio.create_task(self._run(chunks, clean, show))
        try:
            await task
        except asyncio.CancelledError:
            self.stop()
            if asyncio.current_task().cancelling():   # the *caller* was cancelled, not just our speech
                raise

    def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        self.reference.clear()                                              # whatever was queued will not be heard
        if self._task and not self._task.done():
            self._task.cancel()
        self._proc = None

    # --- internals ---
    async def _run(self, chunks: AsyncIterator[str], clean: bool, show: bool = False) -> None:
        self.active_since_first = False
        queue: asyncio.Queue[tuple[Path, str, np.ndarray | None] | None] = asyncio.Queue(maxsize=2)    # synthesize ahead of playback
        shown = ""                       # text already revealed (previous chunks)
        speaking = False
        preroll = self.config.get("tts.preroll_ms", 300)

        async def produce() -> None:
            try:
                async for chunk in chunks:
                    chunk = clean_for_speech(chunk, None) if clean else chunk
                    if chunk:
                        p = await self._synth(chunk, tmp)
                        await queue.put((p, chunk, await asyncio.to_thread(decode_pcm, p)))
            finally:
                await queue.put(None)

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            producer = asyncio.create_task(produce())
            try:
                while (item := await queue.get()) is not None:
                    path, chunk_text, pcm = item
                    if not speaking:
                        speaking = True
                        self.bus.publish("speaking", on=True)
                        if preroll and time.monotonic() - Speaker._last_end > 1.5:
                            await self._play(self._silence(preroll))          # wake the output device
                    if not Speaker.first_audio_at or not self.active_since_first:
                        Speaker.first_audio_at, self.active_since_first = time.monotonic(), True
                    self.spoken.append((time.monotonic(), chunk_text))
                    if pcm is not None:
                        self.reference.add(time.monotonic() + 0.05, pcm)      # afplay starts a moment after we launch it
                    reveal = asyncio.create_task(self._reveal(path, chunk_text, shown)) if show else None
                    try:
                        await self._play(path)
                    finally:
                        if reveal:
                            reveal.cancel()
                    if show:
                        shown = f"{shown} {chunk_text}".strip()
                        self.bus.publish("speech_text", text=shown)     # chunk finished: make sure all of it is visible
            finally:
                producer.cancel()
                if speaking:
                    self.bus.publish("speaking", on=False)

    async def _reveal(self, path: Path, text: str, prefix: str) -> None:
        """Publish the chunk's words as the voice reaches them (offsets come from Edge's word boundaries)."""
        words = text.split()
        try:
            offsets: list[float] = json.loads(path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            offsets = []
        if not offsets or not words:
            self.bus.publish("speech_text", text=f"{prefix} {text}".strip())
            return
        t0 = time.monotonic() + 0.08                      # afplay start-up
        for k, off in enumerate(offsets):
            delay = t0 + off - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            upto = min(len(words), math.ceil((k + 1) / len(offsets) * len(words)))
            self.bus.publish("speech_text", text=f"{prefix} {' '.join(words[:upto])}".strip())

    _n = 0

    CACHE = DATA_DIR / "tts_cache"
    _last_end = 0.0            # when audio last finished playing (monotonic)
    first_audio_at = 0.0       # when the latest speech actually started playing (monotonic), for latency logs

    def prefetch(self, text: str) -> None:
        """Fire-and-forget: synthesize `text` into the cache now, so a later speak() of it starts instantly.
        Used speculatively (the reply may not end up being spoken; the cost is one free synthesis)."""
        if not self.voice_on or len(text) > 120 or self.config.get("tts.backend", "edge") != "edge":
            return
        async def go() -> None:
            try:
                with tempfile.TemporaryDirectory() as d:
                    await self._synth(clean_for_speech(text, None), Path(d))
            except Exception as e:
                log.debug("prefetch failed: %s", e)
        asyncio.get_running_loop().create_task(go())

    def wake(self) -> None:
        """Fire-and-forget: wake the output device now (silent clip) while we think, so the reply's first word
        isn't clipped and no lead-in delay is added later."""
        ms = self.config.get("tts.preroll_ms", 300)
        if not self.voice_on or not ms or time.monotonic() - Speaker._last_end <= 1.5:
            return
        Speaker._last_end = time.monotonic() + ms / 1000          # the real playback will see "just woken"
        async def go() -> None:
            p = await asyncio.create_subprocess_exec("afplay", str(self._silence(ms)),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
        asyncio.get_running_loop().create_task(go())

    def _silence(self, ms: int) -> Path:
        """A short silent WAV. Played first when the output has been idle: speakers/monitors often take a
        moment to wake, which swallows the first syllable of short phrases like "Goodbye." or "Yes?"."""
        path = self.CACHE / f"_silence_{ms}.wav"
        if not path.exists():
            self.CACHE.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(b"\x00\x00" * int(24000 * ms / 1000))
        return path

    def _cache_path(self, text: str, ext: str) -> Path:
        key = hashlib.sha1(f"{self.config.get('tts.voice')}|{self.config.get('tts.rate')}|{text}".encode()).hexdigest()[:16]
        return self.CACHE / f"{key}.{ext}"

    async def _synth(self, text: str, tmp: Path) -> Path:
        """Short phrases are cached on disk, so repeats ("Yes?", "Opening LinkedIn.") play instantly."""
        Speaker._n += 1
        cacheable = len(text) <= 120
        if self.config.get("tts.backend", "edge") == "edge":
            cached = self._cache_path(text, "mp3")
            if cacheable and cached.exists() and cached.stat().st_size > 0:
                return cached
            try:
                if cacheable:
                    self.CACHE.mkdir(parents=True, exist_ok=True)
                    await self._edge(text, cached.with_suffix(".part"))
                    cached.with_suffix(".part").replace(cached)
                    self._prune()
                    return cached
                return await self._edge(text, tmp / f"{Speaker._n}.mp3")
            except Exception as e:      # offline / service hiccup: fall back to the built-in voice
                log.warning("Edge TTS failed (%s); using macOS voice", e)
        return await self._say(text, tmp / f"{Speaker._n}.aiff")

    def _prune(self, keep: int = 300) -> None:
        files = sorted(self.CACHE.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink(missing_ok=True)

    async def prewarm(self, phrases: list[str]) -> None:
        """Synthesize fixed phrases in the background at startup so the first use is instant."""
        if self.config.get("tts.backend", "edge") != "edge":
            return
        with tempfile.TemporaryDirectory() as d:
            for phrase in phrases:
                try:
                    await self._synth(phrase, Path(d))
                except Exception as e:
                    log.debug("prewarm failed for %r: %s", phrase, e)

    async def _edge(self, text: str, path: Path) -> Path:
        import edge_tts
        com = edge_tts.Communicate(text, self.config.get("tts.voice", "en-US-AriaNeural"),
                                   rate=self.config.get("tts.rate", "+0%"), boundary="WordBoundary")
        offsets: list[float] = []
        with open(path, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    offsets.append(chunk["offset"] / 1e7)          # 100 ns ticks -> seconds
        if path.stat().st_size == 0:
            raise RuntimeError("no audio returned")
        path.with_suffix(".json").write_text(json.dumps(offsets))  # word timings travel with the cached audio
        return path

    async def _say(self, text: str, path: Path) -> Path:
        p = await asyncio.create_subprocess_exec("say", "-o", str(path), text)
        await p.wait()
        return path

    async def _play(self, path: Path) -> None:
        self._proc = await asyncio.create_subprocess_exec("afplay", str(path), stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
        await self._proc.wait()
        Speaker._last_end = time.monotonic()
