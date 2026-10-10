from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.registry import Registry
from voicectl.tts.speaker import Speaker


@dataclass
class ModeContext:
    config: Config
    bus: EventBus
    speaker: Speaker | None = None
    shared: dict = field(default_factory=dict)

    async def say(self, mode: str, text: str) -> None:
        """Speak `text` if TTS is on for this mode."""
        if self.speaker and self.speaker.enabled_for(mode):
            await self.speaker.speak(text)


class Mode(ABC):
    """One hotkey = one mode. `name` must match a key under `hotkeys:` in config.yaml."""
    name: str
    continuous: bool = False   # True: the mode runs its own listen/respond loop (see conversation.py)

    def __init__(self, ctx: ModeContext) -> None:
        self.ctx = ctx

    @abstractmethod
    async def handle(self, text: str) -> None: ...


modes: Registry[Mode] = Registry("mode")
