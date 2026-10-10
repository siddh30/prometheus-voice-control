from __future__ import annotations

import asyncio

from voicectl import actions as actions_pkg
from voicectl.actions.base import actions
from voicectl.actions.router import run_action
from voicectl.modes.base import Mode, ModeContext, modes


@modes.register("action")
class ActionMode(Mode):
    name = "action"

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        actions.discover(actions_pkg)

    async def handle(self, text: str) -> None:
        self.ctx.bus.publish("state", state="acting", mode=self.name)
        use_llm = self.ctx.config.get("action.llm_fallback", True)
        result = await asyncio.to_thread(run_action, text, use_llm)
        self.ctx.bus.publish("step", text=result.message, done=result.ok)
        if not result.ok:
            self.ctx.bus.publish("notice", text=result.message)
        await self.ctx.say(self.name, result.message)
