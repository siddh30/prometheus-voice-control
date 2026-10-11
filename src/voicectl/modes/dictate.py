from __future__ import annotations

import asyncio

from voicectl.modes.base import Mode, ModeContext, modes
import os

from voicectl.output.typer import AppTyper, Typer


@modes.register("dictate")
class DictateMode(Mode):
    """F9: you speak, the words are typed at the cursor of whatever app has focus. No reply, no routing."""
    name = "dictate"

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        if os.environ.get("VOICECTL_HOTKEYS") == "app":
            self.typer = AppTyper(ctx.bus)
        else:
            self.typer = Typer(
                method=ctx.config.get("output.typing_method", "clipboard"),
                restore_clipboard=ctx.config.get("output.restore_clipboard", True),
            )

    async def handle(self, text: str) -> None:
        if self.ctx.config.get("swarm.enabled", False) and self.ctx.shared.get("swarm_mode", False):
            # Swarm mode: F9 is the MANUAL key. Transcribe, then let the user choose where it goes (a computer, auto, or typed here).
            self.ctx.bus.publish("swarm_manual", text=text)
            self.ctx.bus.publish("state", state="idle", mode=self.name)
            return
        self.ctx.bus.publish("state", state="typing", mode=self.name)
        await asyncio.to_thread(self.typer.type, text)
