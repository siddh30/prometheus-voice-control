from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from voicectl import platform
from voicectl.core.config import Config


class Engine(Protocol):
    """Turns text into an audio file. Nothing about caching, playback or captions: that is the Speaker's business."""
    ext: str                # file extension of what it writes
    cacheable: bool         # the same text always sounds the same, so short phrases may be cached on disk

    async def synth(self, text: str, path: Path) -> Path: ...


class EdgeEngine:
    """Microsoft Edge neural voices (free, online). Also writes the word timings next to the audio, for the live captions."""
    ext, cacheable = "mp3", True

    def __init__(self, config: Config) -> None:
        self.voice = config.get("tts.voice", "en-US-AriaNeural")
        self.rate = config.get("tts.rate", "+0%")

    async def synth(self, text: str, path: Path) -> Path:
        import edge_tts
        com = edge_tts.Communicate(text, self.voice, rate=self.rate, boundary="WordBoundary")
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


class SystemEngine:
    """The operating system's built-in voice: offline, always available, the fallback for everything else."""
    ext, cacheable = "aiff", False

    async def synth(self, text: str, path: Path) -> Path:
        await platform.current().say_to_file(text, str(path))
        return path


def engine_for(config: Config) -> Engine:
    return EdgeEngine(config) if config.get("tts.backend", "edge") == "edge" else SystemEngine()
