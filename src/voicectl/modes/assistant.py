from __future__ import annotations

import asyncio
import logging
import time

from voicectl import actions as actions_pkg
from voicectl.actions.base import actions
from voicectl.agents.shared import runner_for
from voicectl.modes.base import ModeContext, modes
from voicectl.modes.conversation import ConversationMode, DelegateRequested, sentences
from voicectl.modes.router import build_deciders, fast_route, parse_yes_no, route
from voicectl.tts.speaker import clean_for_speech

log = logging.getLogger("voicectl.assistant")

ACKS = ["On it.", "Sure, on it.", "Got it, working on that.", "Okay, let me do that."]


@modes.register("assistant")
class AssistantMode(ConversationMode):
    """Prometheus: the conversational front of a small team.

    Each thing you say is routed: a simple command runs as an action, real work (browser, files, research) is handed to
    the agent as a BACKGROUND JOB, and everything else is a spoken conversation. While the agent works you can keep
    talking (chatting does not cancel it); Prometheus announces the result when it is done, speaks any approval question,
    answers "how's it going?" from the agent's live status, and stops the job on "stop" or the key. The agent sees the
    conversation, and Prometheus knows what the agent found."""
    name = "assistant"

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        actions.discover(actions_pkg)
        self.deciders = build_deciders(ctx.config)
        self.job: asyncio.Task | None = None
        self.job_text = ""
        self.notes: list[str] = []                 # what the agent finished recently (for chat context)
        self._ack_i = 0
        self._approval_watch: asyncio.Task | None = None

    # ---- lifecycle ----
    async def run(self, mic, stt, initial: str = "") -> None:
        runner = runner_for(self.ctx)
        runner.mode_name = self.name                          # the widget shows the agent's progress under this mode
        asyncio.create_task(runner.warm_up())                  # boot the agent while the user is still talking
        self._approval_watch = asyncio.create_task(self._watch_approvals())
        try:
            await super().run(mic, stt, initial)
        finally:
            self._approval_watch.cancel()
            await self._stop_job(quiet=True)

    # ---- what the other side knows ----
    def agent_status(self) -> str:
        runner = runner_for(self.ctx)
        recent = " Recently finished: " + " | ".join(self.notes[-3:]) if self.notes else ""
        return runner.status() + recent

    def _agent_context(self) -> str:
        """Hand the agent the recent conversation, so "do that for the second one" or "now Google" make sense."""
        lines = [f"{m['role']}: {str(m['content'])[:240]}" for m in self.history[-7:-1]]
        notes = [f"(earlier agent work) {n}" for n in self.notes[-3:]]
        return "\n".join(notes + lines)

    # ---- background job ----
    def _job_running(self) -> bool:
        return self.job is not None and not self.job.done()

    def interrupt(self) -> bool:
        if super().interrupt():                               # key: cut the speech/reply first
            return True
        if self._job_running():                               # nothing speaking: the key stops the running task
            self.job.cancel()
            return True
        return False

    async def _start_job(self, text: str) -> str:
        if self._job_running():                               # "actually, do X instead": replace it (the agent keeps its memory)
            await self._stop_job(quiet=True)
        runner_for(self.ctx).mode_name = self.name
        self.job_text = text
        self.job = asyncio.create_task(self._job_main(text))
        self.ctx.bus.publish("job", active=True, text=text)
        ack = ACKS[self._ack_i % len(ACKS)]
        self._ack_i += 1
        await self._speak(ack)
        return ack

    async def _stop_job(self, quiet: bool = False) -> bool:
        if not self._job_running():
            return False
        self.job.cancel()
        await asyncio.gather(self.job, return_exceptions=True)
        if not quiet:
            self.notes.append(f"Stopped '{self.job_text}' at the user's request")
        return True

    async def _job_main(self, text: str) -> None:
        runner, bus = runner_for(self.ctx), self.ctx.bus
        try:
            answer = await runner.run_turn(text, self._agent_context())
        except asyncio.CancelledError:
            bus.publish("job", active=False, text=text)
            await asyncio.shield(runner.interrupt())          # stops the browser task mid-step
            raise
        except Exception as e:
            log.exception("agent job failed")
            bus.publish("job", active=False, text=text)
            bus.publish("error", message=f"{type(e).__name__}: {e}")
            await self._announce("Sorry, I ran into a problem with that.", None)
            return
        self.notes = (self.notes + [f"Finished '{text}': {answer[:400]}"])[-5:]
        bus.publish("job", active=False, text=text)
        await self._announce(clean_for_speech(answer, self.ctx.config.get("tts.max_chars", 450)), answer)

    async def _announce(self, spoken: str, display: str | None, max_wait: float = 90.0) -> None:
        """Tell the user the outcome, but only when that won't talk over a reply or over them."""
        waited = 0.0
        while (self._busy() or self._user_talking) and waited < max_wait and not self._ending:
            await asyncio.sleep(0.25)
            waited += 0.25
        if not self._ending:
            self._start_turn(None, ack=spoken, display=display)

    async def _watch_approvals(self) -> None:
        """A risky step needs a yes/no: say the question aloud (the widget shows buttons too; "yes"/"no" works)."""
        async for ev in self.ctx.bus.subscribe():
            if ev.type == "approval" and not self._ending:
                self._start_turn(None, ack=f"{ev.data.get('text', 'Okay to continue?').rstrip('?')}? Say yes or no.")

    # ---- one utterance ----
    async def respond(self, text: str) -> str:
        bus, cfg = self.ctx.bus, self.ctx.config
        runner = runner_for(self.ctx)

        if runner.pending_approval and (answer := parse_yes_no(text)) is not None:      # "yes, go ahead" / "no"
            runner.on_ui_command({"command": "approval", "approved": answer})
            reply = "Okay." if answer else "Okay, I won't."
            await self._speak(reply)
            return reply

        job = self.job_text if self._job_running() else None
        # Most turns are conversation, so start the chat reply while the router decides; if it says anything else the
        # speculative reply is dropped. A clean pattern (stop, scroll, open X) skips both: nothing to decide or guess.
        chat = None
        r = fast_route(text, job)
        if r is None:
            if cfg.get("assistant.speculative_chat", True):
                chat = self._start_chat()
            t0 = time.monotonic()
            try:
                r = await route(text, self.history[:-1], cfg, self.deciders, job)
            except BaseException:
                if chat:
                    chat.cancel()
                raise
            log.info("route: %s via %s in %.2fs", r.kind, r.how, time.monotonic() - t0)
        bus.publish("route", kind=r.kind, how=r.how, confidence=r.confidence, action=r.action.name if r.action else None)
        if chat and r.kind != "chat":
            chat.cancel()

        if r.kind == "control":
            if await self._stop_job():
                await self._speak("Okay, stopped.")
                return "Stopped."
            await self._speak("Nothing is running right now.")
            return "Nothing is running."

        if r.kind == "action":
            bus.publish("state", state="acting", mode=self.name)
            result = await asyncio.to_thread(r.action().run, **r.args)
            bus.publish("step", text=result.message, done=result.ok)
            if not result.ok:
                bus.publish("notice", text=result.message)
            bus.publish("result", text=result.message)
            if cfg.get("assistant.confirm_actions", "short") != "silent":
                await self._speak(result.spoken or clean_for_speech(result.message, 120))
            return result.message

        if r.kind == "agent":
            return await self._start_job(text)

        try:
            return await self._reply_and_speak(chat or sentences(self._tokens()))
        except DelegateRequested:                              # the chat model recognised a task: hand it over, don't refuse
            log.info("chat reply delegated to the agent")
            return await self._start_job(text)
        finally:
            if chat:
                chat.cancel()                       # also when you talk over the reply: stop generating, not just speaking

    async def _speak(self, text: str) -> None:
        if text and self.ctx.speaker and self.ctx.config.get("tts.enabled", True):
            await self.ctx.speaker.speak(text)
