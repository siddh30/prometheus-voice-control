from __future__ import annotations

import asyncio
import logging

from voicectl.agents.complexity import heuristic, hint_block
from voicectl.actions.base import actions
from voicectl.actions.router import quick_action
from voicectl.agents.runner import AgentRunner, Finished
from voicectl import actions as actions_pkg
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory
from voicectl.swarm.worker import TaskContext

log = logging.getLogger("voicectl.swarm.executor")

PROMPT = "prometheus_prompt.txt"          # single mode's agent prompt: browsing playbook, what to do when setup is missing, approvals


class AgentExecutor:
    """Runs the normal Prometheus agent for a swarm task, headless: its own private event bus, no voice, no mic, no widget.
    The agent is unchanged; this only translates what it publishes (checklist, steps, approvals) into worker events."""

    def __init__(self, config: Config, memory: Memory) -> None:
        self.config = config
        self.bus = EventBus()
        # the same agent, with the same playbook, as single mode (not the bare default prompt); the daemon's one shared Memory;
        # screen=None: there is no app on a worker to look through
        self.runner = AgentRunner(config, self.bus, memory, prompt_name=PROMPT)
        actions.discover(actions_pkg)
        self.runner.mode_name = "swarm"
        self._lock = asyncio.Lock()               # one agent session: a second task waits for the first (the controller replaces, not stacks)

    async def run(self, task: TaskContext) -> str:
        if (instant := await self._instant(task)) is not None:
            return instant
        async with self._lock:
            relay = asyncio.create_task(self._relay(task))
            await asyncio.sleep(0)                # the relay must be subscribed before the agent starts publishing
            try:
                complexity = heuristic(task.text)
                final = ""
                async for ev in self.runner.stream_turn(task.text, task.context, hint_block(complexity), complexity.level,
                                                        publish_result=False):
                    if isinstance(ev, Finished):
                        final = ev.text           # words are not spoken on a worker; the final text is the answer
                return final.strip()
            finally:
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)

    async def _instant(self, task: TaskContext) -> str | None:
        """Exact one-step commands ("open linkedin", "volume 30") run at once with no model and no browser automation, exactly as
        in single mode. Any doubt or failure returns None and the agent takes over."""
        hit = quick_action(task.text)
        if hit is None:
            return None
        cls, args = hit
        try:
            result = await asyncio.to_thread(lambda: cls().run(**args))
        except Exception:
            log.info("instant action %s failed; handing it to the agent", cls.name, exc_info=True)
            return None
        if not result.ok:
            return None
        task.emit("step", text=(result.spoken or result.message).rstrip("."), done=True)
        return result.spoken or result.message

    async def _relay(self, task: TaskContext) -> None:
        async for ev in self.bus.subscribe():
            if ev.type == "checklist":
                task.emit("checklist", items=list(ev.data.get("items") or []))
            elif ev.type == "step":
                task.emit("step", text=str(ev.data.get("text", "")), done=bool(ev.data.get("done")), failed=bool(ev.data.get("failed")))
            elif ev.type == "approval":
                asyncio.get_running_loop().create_task(self._approve(task, str(ev.data.get("text", "Approve this action?"))))

    async def _approve(self, task: TaskContext, question: str) -> None:
        try:
            ok = await task.ask_approval(question)
        except asyncio.CancelledError:
            ok = False
        self.runner.respond_approval(ok)

    async def cancel(self, task_id: str) -> None:
        await self.runner.interrupt()
