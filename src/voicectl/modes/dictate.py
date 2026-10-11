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
        self.ctx.bus.publish("state", state="typing", mode=self.name)
        await asyncio.to_thread(self.typer.type, text)
