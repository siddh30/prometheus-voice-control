from __future__ import annotations

import numpy as np

from voicectl.stt.base import STTBackend, backends


@backends.register("faster_whisper")
class FasterWhisperBackend(STTBackend):
    def __init__(self, config) -> None:
        super().__init__(config)
        self._model = None

    def warm_up(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.config.get("stt.faster_whisper.model", "small.en"),
                device="cpu",
                compute_type=self.config.get("stt.faster_whisper.compute_type", "int8"),
                cpu_threads=self.config.get("stt.faster_whisper.cpu_threads", 4),   # 8 measured slower on this Mac
            )

    def transcribe(self, audio: np.ndarray, sample_rate: int, hint: str | None = None) -> str:
        self.warm_up()
        words = " ".join(filter(None, [hint, *self.config.get("stt.hotwords", [])])) or None   # bias names/jargon
        segments, _ = self._model.transcribe(
            audio, language="en", beam_size=1, hotwords=words, without_timestamps=True,
            condition_on_previous_text=False, vad_filter=self.config.get("stt.faster_whisper.vad_filter", True))
        return " ".join(s.text.strip() for s in segments).strip()

