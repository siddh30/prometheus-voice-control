from __future__ import annotations

import asyncio

from voicectl.agents.runner import AgentRunner
from voicectl.agents.screen import Screen, ScreenLink
from voicectl.core.config import DATA_DIR
from voicectl.core.memory import Memory
from voicectl.modes.base import ModeContext


def runner_for(ctx: ModeContext, prompt_name: str = "agent_prompt.txt") -> AgentRunner:
    """One AgentRunner per daemon, shared by every mode that can run agent tasks (one browser, one session)."""
    if "agent_runner" not in ctx.shared:
        link = ScreenLink(ctx.bus)
        link.start()
        runner = AgentRunner(ctx.config, ctx.bus, Memory(DATA_DIR / "voicectl.db"), screen=Screen(link), prompt_name=prompt_name)
        ctx.shared["agent_runner"] = runner

        async def listen_ui() -> None:
            async for ev in ctx.bus.subscribe():
                if ev.type == "ui_command":
                    runner.on_ui_command(ev.data)
        ctx.shared["ui_listener"] = asyncio.get_running_loop().create_task(listen_ui())
    return ctx.shared["agent_runner"]
