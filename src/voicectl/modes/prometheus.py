from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time

from voicectl import actions as actions_pkg
from voicectl.actions.base import actions
from voicectl.agents.complexity import Complexity, hint_block, heuristic
from voicectl.agents.runner import Finished, TextDelta, TextEnd, TextStart
from voicectl.agents.shared import runner_for
from voicectl.core.wake import find_wake
from voicectl.modes.base import ModeContext, modes
from voicectl.modes.conversation import ConversationMode, sentences
from voicectl.modes.router import STOP, JevDecider, fast_route, normalize, parse_yes_no
from voicectl.tts.speaker import clean_for_speech, strip_emoji

log = logging.getLogger("voicectl.prometheus")

# An audio request: the user asked for something to be played/listened to. Only then may we go into quiet, name-only listening.
AUDIO_INTENT = re.compile(
    r"\b(play|put on|queue(?: up)?|listen to|stream|shuffle|turn on (?:some )?music)\b"
    r"|\b(song|songs|music|playlist|podcast|album|track|radio|soundtrack|audiobook|mixtape|karaoke)\b"
    r"|\bon (?:youtube|spotify|soundcloud|apple music|pandora|deezer)\b", re.I)
VOICE_OFF = re.compile(r"^(?:please\s+)?(?:(?:go|switch|stay|be)\s+(?:to\s+|in\s+)?(?:text(?:\s+only|\s+mode)?|silent|quiet)|text only|text mode|"
                       r"mute(?:\s+your)?\s+voice|stop\s+(?:talking|speaking)\s+(?:out\s+loud|aloud)|(?:don'?t|do not)\s+(?:talk|speak)\s+(?:out\s+loud|aloud))\W*$", re.I)
VOICE_ON = re.compile(r"^(?:please\s+)?(?:(?:speak|talk)\s+(?:to\s+me(?:\s+again)?|out\s+loud|aloud|again)|voice\s+on|turn\s+(?:your\s+)?voice\s+(?:back\s+)?on|"
                      r"unmute(?:\s+your)?\s+voice|use\s+(?:your\s+)?voice)\W*$", re.I)
FRESH = re.compile(r"^(?:please\s+)?(?:forget\s+(?:everything|all(?:\s+of)?\s+(?:it|that|this|of\s+it))(?:\s+(?:we|you)\s+(?:know|talked|discussed|said))?|"
                   r"start\s+(?:fresh|over|from\s+scratch|a\s+new\s+(?:session|conversation))|(?:wipe|erase|delete|clear)\s+(?:out\s+)?(?:all\s+(?:of\s+)?)?(?:your|the|my)?\s*(?:all\s+)?memor(?:y|ies)|"
                   r"(?:wipe|erase)\s+(?:everything|it\s+all)|clear\s+everything)(?:\s+(?:please|now))?\W*$", re.I)
NEW_CHAT = re.compile(r"^(?:please\s+)?(?:(?:start|begin|open)\s+(?:a\s+)?(?:new|fresh|clean)\s+(?:conversation|chat|session|context|thread)|new\s+(?:conversation|chat|session|thread)|"
                      r"(?:reset|refresh|clear)\s+(?:the\s+|this\s+|your\s+)?(?:conversation|chat|context|thread)|(?:you(?:'re|\s+are)\s+)?stuck(?:,?\s+(?:reset|refresh)(?:\s+(?:it|yourself))?)?)(?:\s+(?:please|now))?\W*$", re.I)
FORGET_LEARNED = re.compile(r"^(?:please\s+)?forget\s+(?:(?:everything|all|what)\s+)?(?:(?:that\s+)?you(?:'ve|\s+have)?\s+learn(?:ed|t)|learn(?:ed|t)|your\s+learnings|the\s+lessons)(?:\s+(?:please|now))?\W*$", re.I)
CLEAN_MEMORY = re.compile(r"^(?:please\s+)?(?:clean|clear|tidy|refresh|reset)(?:\s+up)?(?:\s+(?:your|the|my))?\s+(?:memory|context)\W*$", re.I)


class _Block:
    """One text block of the model's reply, fed word by word and spoken sentence by sentence."""

    def __init__(self) -> None:
        self.q: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, text: str) -> None:
        self.q.put_nowait(text)

    def close(self) -> None:
        self.q.put_nowait(None)

    async def pieces(self):
        while (item := await self.q.get()) is not None:
            yield item


class _Voice:
    """Speaks the agent's words in order, as they stream in, while tools keep running in parallel. That is what makes
    Prometheus narrate a task ("Opening YouTube... found it...") instead of going quiet until the end."""

    def __init__(self, ctx: ModeContext) -> None:
        self.ctx = ctx
        self.enabled = bool(ctx.speaker and ctx.config.get("tts.enabled", True))
        self.blocks: dict[int, _Block] = {}
        self.queue: asyncio.Queue[_Block | None] = asyncio.Queue()
        self.text: dict[int, list[str]] = {}
        self.recent: list[str] = []                    # what was just said this turn, so a status is never said twice
        self.worker = asyncio.create_task(self._work()) if self.enabled else None

    def is_repeat(self, sentence: str) -> bool:
        """"Playing it now." said before the tool call and again after it is one status, not two."""
        norm = re.sub(r"[^a-z0-9 ]", "", sentence.lower()).strip()
        if not norm:
            return True
        repeat = any(norm == old or difflib.SequenceMatcher(None, norm, old).ratio() >= 0.85 for old in self.recent[-4:])
        self.recent.append(norm)
        return repeat

    async def _fresh(self, stream):
        async for sentence in stream:
            if not self.is_repeat(sentence):
                yield sentence

    async def _work(self) -> None:
        while (block := await self.queue.get()) is not None:
            await self.ctx.speaker.speak_many(self._fresh(sentences(block.pieces())), show=True)

    def start(self, bid: int) -> None:
        self.blocks[bid], self.text[bid] = _Block(), []
        if self.enabled:
            self.queue.put_nowait(self.blocks[bid])

    def delta(self, bid: int, text: str) -> None:
        text = strip_emoji(text)
        self.text[bid].append(text)
        if self.enabled:
            self.blocks[bid].feed(text)
        else:                                          # no voice: just show the words as they arrive
            self.ctx.bus.publish("result", text="".join("".join(t) for t in self.text.values()))

    def end(self, bid: int) -> None:
        if self.enabled:
            self.blocks[bid].close()

    async def drain(self) -> None:
        if self.worker:
            self.queue.put_nowait(None)
            await self.worker

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
        if self.ctx.speaker:
            self.ctx.speaker.stop()


@modes.register("prometheus")
class PrometheusMode(ConversationMode):
    """Prometheus: ONE agent that both converses and acts.

    Everything you say goes to the same Claude agent (one brain, one memory) which can answer, browse your own Chrome,
    look at the screen and use the mouse, read your files, and run quick actions. Its words are spoken as they are
    written, so it narrates what it is doing; its checklist is shown on screen. Talking over it interrupts it and your
    new sentence becomes the newest instruction. Only exact one-step commands ("open Safari", volume 30) skip the model."""
    name = "prometheus"
    PROMPT = "prometheus_prompt.txt"

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        actions.discover(actions_pkg)
        self._approval_watch: asyncio.Task | None = None
        self.music: dict | None = None             # while a song/video plays: keep listening, but only act when addressed by name
        self._attend_until = 0.0
        self._music_watch: asyncio.Task | None = None
        self._side_notes: list[str] = []          # things done outside the agent (instant actions) it should know about
        self.jev = (JevDecider(min_confidence=ctx.config.get("assistant.jev.instant_confidence", 0.85), timeout_s=0.7,
                               allow_extraction=False)
                    if JevDecider.available() and ctx.config.get("assistant.jev.instant", True) else None)

    def _runner(self):
        runner = runner_for(self.ctx, self.PROMPT)
        runner.mode_name = self.name
        return runner

    # ---- lifecycle ----
    async def run(self, mic, stt, initial: str = "") -> None:
        runner = self._runner()
        asyncio.create_task(runner.warm_up())          # boot the agent while the user is still talking
        self._approval_watch = asyncio.create_task(self._watch_approvals())
        try:
            await super().run(mic, stt, initial)
        finally:
            self._approval_watch.cancel()
            await runner.interrupt()                   # never leave a task running after the conversation ends

    def agent_status(self) -> str:
        return self._runner().status()

    # ---- music mode: the song keeps playing and we keep listening, but only act when addressed by name ----
    def holding(self) -> bool:
        return self.music is not None

    def interrupt(self) -> bool:
        if super().interrupt():
            return True
        if self.music is not None:                       # the key during music = "the next thing I say is for you" (no name needed)
            self._attend_until = self.ctx.config and time.monotonic() + self.ctx.config.get("music.attend_s", 8.0)
            self._state("music")
            self.ctx.bus.publish("attend", on=True)
            return True
        return False

    async def filter_utterance(self, text: str) -> str | None:
        """While music plays, lyrics and background sound must not be taken for commands: only "Prometheus, ..." counts
        (or anything said within a few seconds of pressing the key). Everything else is ignored silently."""
        if self.music is None:
            return text
        if time.monotonic() < self._attend_until:
            self._attend_until = 0.0
            self.ctx.bus.publish("attend", on=False)
            return text
        found, rest = find_wake(text, self.ctx.config.get("wake_word.word", "prometheus"))
        if not found:
            log.debug("music mode: ignoring %r (not addressed to me)", text[:50])
            return None
        if not rest.strip():                              # just the name: "yes?", and the next sentence needs no name
            self._attend_until = time.monotonic() + self.ctx.config.get("music.attend_s", 8.0)
            self.ctx.bus.publish("attend", on=True)
            self._start_turn(None, ack="Yes, go ahead." if self._attend_until else "I'm here.")
            return None
        return rest

    @staticmethod
    def _parse_media(raw: str) -> dict | None:
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else None
        except (ValueError, TypeError):
            return None                                  # "NO_MEDIA" and friends

    async def _media_state(self) -> dict | None:
        browser = getattr(self._runner(), "browser", None)
        if browser is None or not getattr(browser, "has_page", lambda: False)():
            return None
        try:
            return self._parse_media(await browser.media("status"))
        except Exception as e:
            log.debug("media status unavailable: %s", e)
            return None

    async def _check_music(self, audio_request: bool = False) -> None:
        """After a task: if the user ASKED for audio and a real song/video is now playing in our tab, enter music mode
        (it keeps playing; we only act on what starts with our name). Audio that merely happens to be playing never does."""
        if not audio_request:
            return
        st = await self._media_state()
        if st and st.get("playing") and not st.get("muted") and st.get("of", 0) >= 45:
            self._enter_music(getattr(getattr(self._runner(), "browser", None), "last_title", ""))

    def _enter_music(self, title: str) -> None:
        if self.music is not None:
            return
        self.music = {"title": title or "music"}
        self.ctx.shared["music_playing"] = True
        self._state("music")
        self.ctx.bus.publish("music", on=True, title=self.music["title"])
        log.info("music mode: %r is playing; I only act on what starts with my name (or after the key)", self.music["title"][:60])
        self._music_watch = asyncio.create_task(self._watch_music())

    def _exit_music(self) -> None:
        if self.music is None:
            return
        self.music = None
        self._attend_until = 0.0
        self.ctx.shared["music_playing"] = False
        self.ctx.bus.publish("music", on=False, title="")
        if not self._ending:
            self._state("listening")

    async def _watch_music(self) -> None:
        """The song ended, the tab closed or someone paused it: back to normal listening."""
        while self.music is not None:
            await asyncio.sleep(6)
            st = await self._media_state()
            if not (st and st.get("playing")):
                self._exit_music()
                return

    # ---- approvals by voice ----
    def keep_turn_on_speech(self) -> bool:
        return self._runner().pending_approval         # your "yes" must not cancel the very step waiting for it

    async def _watch_approvals(self) -> None:
        """A risky step needs a yes or no: ask aloud (the widget shows buttons too)."""
        async for ev in self.ctx.bus.subscribe():
            if ev.type == "approval" and not self._ending and self.ctx.speaker:
                question = clean_for_speech(ev.data.get("text", "Okay to continue?")).rstrip("?")
                asyncio.create_task(self.ctx.speaker.speak(f"{question}? Say yes or no."))

    async def _new_chat(self) -> None:
        await self.ctx.shared["maintenance"].new_conversation(self.ctx.shared.get("forget_conversation", lambda: None))
        self.ctx.bus.publish("result", text="Okay, new conversation. Clean slate.")
        if self.ctx.speaker:
            await self.ctx.speaker.speak("Okay, new conversation. What's next?")

    async def _start_fresh(self) -> None:
        await self.ctx.shared["maintenance"].start_fresh(self.ctx.shared.get("forget_conversation", lambda: None))
        self.ctx.bus.publish("result", text="Done. I've forgotten everything. We're starting fresh.")
        if self.ctx.speaker:
            await self.ctx.speaker.speak("Done. I've forgotten everything, so we're starting fresh.")

    async def _manual_clean(self) -> None:
        maint = self.ctx.shared.get("maintenance")
        if maint is None:
            return
        report = await maint.clean(force=True)
        log.info("memory cleanup on request: %s", report)
        self.ctx.bus.publish("notice", text=f"Memory cleaned: {report}")
        if self.ctx.speaker:
            await self.ctx.speaker.speak("Done. I've tidied my memory and I'll start fresh.")

    async def intercept(self, text: str) -> bool:
        runner = self._runner()
        spk = self.ctx.speaker
        if spk is not None and hasattr(spk, "set_voice"):
            if VOICE_OFF.match(text.strip()):
                spk.set_voice(False)
                self.ctx.bus.publish("result", text="Okay, text only. I'll stay silent.")
                return True
            if VOICE_ON.match(text.strip()):
                spk.set_voice(True)
                self.ctx.bus.publish("result", text="Voice is back on.")
                await spk.speak("Voice is back on.")
                return True
        if FORGET_LEARNED.match(text.strip()) and self.ctx.shared.get("maintenance") is not None and not self._busy():
            async def forget() -> None:
                await self.ctx.shared["maintenance"].forget_learnings()
                self.ctx.bus.publish("result", text="Done. I've forgotten what I'd learned about how to do things.")
                if self.ctx.speaker:
                    await self.ctx.speaker.speak("Done. I've forgotten what I'd learned about how to do things.")
            asyncio.create_task(forget())
            return True
        if NEW_CHAT.match(text.strip()) and self.ctx.shared.get("maintenance") is not None:
            self._cut_off()
            asyncio.create_task(self._new_chat())
            return True
        if FRESH.match(text.strip()) and self.ctx.shared.get("maintenance") is not None and not self._busy():
            asyncio.create_task(self._start_fresh())
            return True
        if CLEAN_MEMORY.match(text.strip()) and self.ctx.shared.get("maintenance") is not None and not self._busy():
            asyncio.create_task(self._manual_clean())
            return True
        if runner.pending_approval and (answer := parse_yes_no(text)) is not None:
            runner.on_ui_command({"command": "approval", "approved": answer})
            if self.ctx.speaker:
                asyncio.create_task(self.ctx.speaker.speak("Okay." if answer else "Okay, I won't."))
            return True
        if self._busy() and STOP.match(normalize(text)):
            self._cut_off()                            # cancels the turn; its handler interrupts the agent
            self._start_turn(None, ack="Okay, stopped.")
            return True
        return False

    # ---- one utterance ----
    async def respond(self, text: str) -> str:
        # Exact one-step commands ("open Safari", "volume 30") don't need the model: instant.
        fast = fast_route(text)
        if fast is not None and fast.kind == "action":
            return await self._quick_action(fast)
        if fast is not None:                                   # "scroll down", "go back": a one-step follow-up on the page
            return await self._agent_turn(text, Complexity("single", "pattern"))
        # One Jev call (~140 ms) answers two things: is this a clearly instant command, and how big is the request?
        # It only acts on an instant command when it is confident AND the arguments are exact; every doubt goes to the agent.
        read = await self._jev_read(text)
        if read is not None:
            instant = self._instant_action(text, read)
            if instant is not None:
                log.info("instant action via Jev: %s %s (%.2f)", instant.action.name, instant.args, instant.confidence or 0)
                return await self._quick_action(instant)
        return await self._agent_turn(text, self._complexity(text, read))

    async def _jev_read(self, text: str):
        if not self.jev:
            return None
        try:
            return await self.jev.read(text, [], None)
        except Exception as e:
            log.debug("jev read failed: %s", e)
            return None

    def _instant_action(self, text: str, read):
        from voicectl.modes.router import Route
        conf = self.jev.min_confidence
        if len(text.split()) > 10 or read.route != "action" or read.route_conf < conf or read.action_conf < conf:
            return None
        if read.action is None or read.action not in actions.all():
            return None
        cls = actions.get(read.action)
        args = cls.match(text) if cls.params else {}
        if args is None:                                       # arguments would have to be guessed: let the agent handle it
            return None
        return Route("action", cls, args, how="jev", confidence=min(read.route_conf, read.action_conf))

    def _complexity(self, text: str, read) -> Complexity:
        if read is not None and read.complexity in ("none", "single", "multi", "extended") and read.complexity_conf >= 0.5:
            return Complexity(read.complexity, "jev", read.complexity_conf)
        return heuristic(text)

    async def _quick_action(self, r) -> str:
        bus, cfg = self.ctx.bus, self.ctx.config
        bus.publish("state", state="acting", mode=self.name)
        result = await asyncio.to_thread(r.action().run, **r.args)
        bus.publish("step", text=result.message, done=result.ok)
        bus.publish("result", text=result.message)
        if not result.ok:
            bus.publish("notice", text=result.message)
        self._side_notes.append(f"Prometheus did: {result.message}")
        if cfg.get("assistant.confirm_actions", "short") != "silent" and self.ctx.speaker and cfg.get("tts.enabled", True):
            await self.ctx.speaker.speak(result.spoken or clean_for_speech(result.message, 120))
        return result.message

    async def _agent_turn(self, text: str, complexity: Complexity | None = None) -> str:
        runner = self._runner()
        voice, final = _Voice(self.ctx), ""
        context, self._side_notes = "\n".join(self._side_notes[-5:]), []
        try:
            c = complexity or heuristic(text)
            log.info("task complexity: %s (%s)", c.level, c.source)
            async for ev in runner.stream_turn(text, context, hint_block(c), c.level, publish_result=False):
                if isinstance(ev, TextStart):
                    voice.start(ev.id)
                elif isinstance(ev, TextDelta):
                    voice.delta(ev.id, ev.text)
                elif isinstance(ev, TextEnd):
                    voice.end(ev.id)
                elif isinstance(ev, Finished):
                    final = ev.text
            await voice.drain()
        except asyncio.CancelledError:                 # talked over, key pressed, or conversation ended
            voice.cancel()
            await asyncio.shield(runner.interrupt())
            raise
        except BaseException:
            voice.cancel()
            raise
        self.ctx.bus.publish("result", text=strip_emoji(final))     # the whole answer stays on screen after the last spoken block
        await self._check_music(audio_request=bool(AUDIO_INTENT.search(text)) or getattr(runner, "played_media", False))
        return final
