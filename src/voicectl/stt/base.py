from __future__ import annotations

import re
from abc import ABC, abstractmethod

import numpy as np

from voicectl.core.config import Config
from voicectl.core.registry import Registry


def vocabulary(config: Config, hint: str | None = None) -> list[str]:
    """The names and jargon the speech model should spell right (`stt.hotwords`), after the caller's hint."""
    return [w for w in [hint, *config.get("stt.hotwords", [])] if w]


def correct(text: str, config: Config) -> str:
    """Fix what the model reliably mishears (`stt.corrections`: heard -> meant, whole words, any case, longest first)."""
    fixes = config.get("stt.corrections", {}) or {}
    for heard in sorted(fixes, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(heard)}\b", fixes[heard], text, flags=re.I)
    return text


class STTBackend(ABC):
    """Blocking transcription of mono float32 audio at 16 kHz. Run via asyncio.to_thread."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def warm_up(self) -> None:
        """Optional: load models ahead of the first request."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int, hint: str | None = None) -> str: ...


backends: Registry[STTBackend] = Registry("stt backend")
