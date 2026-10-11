"""Input channels: the two separate ways a request reaches the assistant. Each knows only its own business and
hands the core plain text; the core never asks which one it came from.
  VoiceChannel: microphone -> speech detection -> transcription, echo and mic health.
  TextChannel:  the type box; also owns the exclusive 'typing mode' during which voice is ignored."""
from voicectl.channels.text import TextChannel
from voicectl.channels.voice import VoiceChannel

__all__ = ["TextChannel", "VoiceChannel"]
