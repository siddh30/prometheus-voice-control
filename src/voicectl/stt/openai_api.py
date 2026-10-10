from __future__ import annotations

import io
import re
import wave

import numpy as np

from voicectl.stt.base import STTBackend, backends


# What speech models "hear" in silence and noise (they were trained on subtitled videos).
HALLUCINATIONS = re.compile(r"thanks? for watching|thank you for watching|like and subscribe|please subscribe|subscribe to (my|the|our)|see you (in|on) the next|"
                            r"subtitles? by|amara\.org|www\.|\.com\b|^\W*(\[?music\]?|\(?applause\)?|you)\W*$", re.I)


def clean_transcript(text: str, hint: str | None = None, language: str | None = "en") -> str:
    """Drop what is not speech: invented subtitle phrases, the prompt echoed back, and (when English is expected) text in other scripts."""
    text = (text or "").strip()
    if not text or HALLUCINATIONS.search(text):
        return ""
    if hint and re.sub(r"\W+", "", text).lower() == re.sub(r"\W+", "", hint).lower():
        return ""
    if language == "en":
        letters = [c for c in text if c.isalpha()]
        if letters and sum(c.isascii() or c in "éèêëáàâäíìîïóòôöúùûüñç" for c in letters) / len(letters) < 0.6:
            return ""
    return text


@backends.register("openai")
class OpenAIBackend(STTBackend):
    def transcribe(self, audio: np.ndarray, sample_rate: int, hint: str | None = None) -> str:
        from openai import OpenAI

        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm.tobytes())
        buf.name = "audio.wav"
        buf.seek(0)
        lang = self.config.get("stt.language", "en")          # without this, noise gets "transcribed" as Greek, Hindi, Korean...
        model = self.config.get("stt.openai.model", "whisper-1")
        use_prompt = bool(hint) and model == "whisper-1"      # the gpt-4o models treat a prompt as text to say back on noise
        result = OpenAI().audio.transcriptions.create(
            model=model, file=buf, **({"language": lang} if lang else {}), **({"prompt": hint} if use_prompt else {})
        )
        return clean_transcript(result.text, hint, lang)
