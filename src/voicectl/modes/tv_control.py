from __future__ import annotations

import asyncio
import logging

from voicectl.modes.base import Mode, ModeContext, modes
from voicectl.tv import planner
from voicectl.tv.client import TvClient, TvUnavailable
from voicectl.tv.commands import TvCommand, parse_many
from voicectl.tv.display import DisplayClient, DisplayError

log = logging.getLogger(__name__)


@modes.register("tv_control")
class TvControlMode(Mode):
    """TV control (the TV app's Ctrl+F9): you say what you want, and the TV does it. Plain commands and chains ("open YouTube and
    turn the volume down") go through a fixed allow-list; anything the parser does not know goes to a model that can only fill in a
    plan form made of the same kinds of steps, which is checked in code. Whatever fails the check is refused out loud."""
    name = "tv_control"

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        self.client = TvClient()
        self.display = DisplayClient()

    async def _run(self, cmd: TvCommand) -> str:
        """One step. The screen steps work on this Mac; everything else goes to the TV. Returns what the step has to say, or ''."""
        cfg = self.ctx.config
        if cmd.kind == "extend_display":
            return await self.display.extend(cfg.get("tv.airplay_name") or "", list(cfg.get("tv.work_apps") or []), cfg.get("tv.airplay_id") or "")
        if cmd.kind == "mirror_display":
            return await self.display.mirror_main(cfg.get("tv.airplay_name") or "", cfg.get("tv.airplay_id") or "")
        if cmd.kind == "stop_extending":
            said = await self.display.stop(cfg.get("tv.airplay_name") or "", cfg.get("tv.airplay_id") or "", list(cfg.get("tv.work_apps") or []))
            await self.client.run(TvCommand("key", "HOME"))              # the TV goes back to its own home screen
            return said
        await self.client.run(cmd)
        return ""

    async def _commands(self, text: str) -> tuple[list[TvCommand], str] | None:
        cfg = self.ctx.config
        apps, inputs = cfg.get("tv.apps") or {}, cfg.get("tv.inputs") or {}
        steps = int(cfg.get("tv.max_volume_steps", 10))
        cmds = parse_many(text, apps=apps, inputs=inputs, max_steps=steps)
        if cmds:
            return cmds, ", then ".join(c.label for c in cmds if c.label)
        if not cfg.get("tv.ai", True):
            return None
        self.ctx.bus.publish("state", state="thinking", mode=self.name)
        try:
            return await asyncio.to_thread(planner.plan, text, apps, inputs, int(cfg.get("tv.max_plan_steps", 6)),
                                           cfg.get("tv.ai_model", "claude-haiku-4-5"))
        except Exception:                                   # no key, no network, a malformed reply: refuse, never guess
            log.exception("tv planner failed")
            return None

    async def handle(self, text: str) -> None:
        bus = self.ctx.bus
        bus.publish("state", state="acting", mode=self.name)
        found = await self._commands(text)
        if found is None:
            message = "I can't do that on the TV."
            bus.publish("notice", text=message)
            await self.ctx.say(self.name, message)
            return
        cmds, summary = found
        bus.publish("state", state="acting", mode=self.name)
        bus.publish("step", text=summary or "Working on it", done=False)
        said = ""
        try:
            for cmd in cmds:
                said = await self._run(cmd) or said
        except (TvUnavailable, DisplayError) as e:
            message = str(e)
            bus.publish("step", text=message, done=False, failed=True)
            bus.publish("notice", text=message)
        else:
            message = said or summary or "Done"                          # a screen step reports what it really did
            bus.publish("step", text=message, done=True)
        await self.ctx.say(self.name, message)
