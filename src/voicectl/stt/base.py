from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from voicectl.core.config import Config
from voicectl.core.registry import Registry


class STTBackend(ABC):
    """Blocking transcription of mono float32 audio at 16 kHz. Run via asyncio.to_thread."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def warm_up(self) -> None:
        """Optional: load models ahead of the first request."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int, hint: str | None = None) -> str: ...


backends: Registry[STTBackend] = Registry("stt backend")
