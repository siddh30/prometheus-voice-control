from __future__ import annotations

from voicectl.agents.runner import AgentRunner
from voicectl.api import Control, RequestBroker
from voicectl.agents.screen import Screen, ScreenLink
from voicectl.core.config import DATA_DIR
from voicectl.core.memory import Memory
from voicectl.modes.base import ModeContext


def runner_for(ctx: ModeContext, prompt_name: str = "agent_prompt.txt") -> AgentRunner:
    """One AgentRunner per daemon, shared by every mode that can run agent tasks (one browser, one session)."""
    if "agent_runner" not in ctx.shared:
        control = ctx.shared.setdefault("control", Control(ctx.bus))        # the daemon's one command dispatcher
        broker = ctx.shared.setdefault("requests", RequestBroker(ctx.bus))
        runner = AgentRunner(ctx.config, ctx.bus, Memory(DATA_DIR / "voicectl.db"), screen=Screen(ScreenLink(broker)), prompt_name=prompt_name)
        ctx.shared["agent_runner"] = runner
        control.on("input_result", broker.resolve)                          # the app's answer to a screenshot / click / key request
        control.on("approval", lambda cmd: runner.respond_approval(cmd["approved"]))
        control.start()
    return ctx.shared["agent_runner"]
