from __future__ import annotations

import asyncio
import logging

from voicectl.modes.base import modes
from voicectl.modes.conversation import ConversationMode
from voicectl.modes.router import parse_yes_no

log = logging.getLogger("voicectl.swarm")


@modes.register("swarm")
class SwarmMode(ConversationMode):
    """F10 in the swarm app. Same push-to-talk as single mode: press, talk, press. Your words are transcribed once, here, and the
    router sends each part to the right computer. Replies are silent (the widget's box shows every computer's progress);
    the only thing spoken is a question when some computer needs your approval."""
    name = "swarm"

    def _controller(self):
        controller = self.ctx.shared.get("swarm_controller")
        if controller is None:
            raise RuntimeError("The swarm isn't set up. Turn on `swarm.enabled` and list your computers in config.local.yaml.")
        return controller

    async def respond(self, text: str) -> str:
        controller = self._controller()
        watcher = asyncio.create_task(self._announce_approvals(controller))
        try:
            return await controller.run(text, self._recent())
        finally:
            watcher.cancel()

    def _recent(self) -> str:
        """The last few exchanges, so "now do the same on the other one" makes sense to whichever computer gets it."""
        return "\n".join(f"{m['role']}: {str(m['content'])[:240]}" for m in self.history[-7:-1])

    async def _announce_approvals(self, controller) -> None:
        said = ""
        while True:
            await asyncio.sleep(0.3)
            asking = controller.pending_approval
            if asking and asking != said:
                said = asking
                await self._say(f"{asking} Say yes or no.")
            elif not asking:
                said = ""

    # ---- approvals by voice or by typing ----
    def keep_turn_on_speech(self) -> bool:
        controller = self.ctx.shared.get("swarm_controller")
        return bool(controller and controller.pending_approval)           # your "yes" must not cancel the very step waiting for it

    async def intercept(self, text: str) -> bool:
        controller = self.ctx.shared.get("swarm_controller")
        if controller and controller.pending_approval and (answer := parse_yes_no(text)) is not None:
            await controller.approve(answer)
            return True
        return False

    def agent_status(self) -> str:
        controller = self.ctx.shared.get("swarm_controller")
        if not controller:
            return ""
        return "; ".join(f"{s.name} is {s.state}" + (f" ({s.task})" if s.task else "") for s in controller.board.pcs.values())
