from __future__ import annotations

from voicectl.agents.shared import runner_for
from voicectl.modes.base import Mode, ModeContext, modes


@modes.register("agent")
class AgentMode(Mode):
    """Single-shot agent task (bind a key to `agent` in config.yaml to use it on its own)."""
    name = "agent"

    async def handle(self, text: str) -> None:
        answer = await runner_for(self.ctx).run_turn(text)
        await self.ctx.say(self.name, answer)
