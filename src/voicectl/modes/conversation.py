from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from collections.abc import AsyncIterator

from voicectl.core.config import DATA_DIR
from voicectl.channels import TextChannel, VoiceChannel
from voicectl.core.ears import Utterance
from voicectl.core.memory import Memory
from voicectl.core.timeline import TurnTimeline
from voicectl.telemetry import phoenix as tel
from voicectl.modes.base import Mode, ModeContext, modes

log = logging.getLogger("voicectl.conversation")

SYSTEM = """Your name is Prometheus. You are the voice of a small team and you are having a spoken, hands-free conversation with the user. Your replies are read aloud.
- Talk like a thoughtful person: short, natural sentences. Usually 1-3 sentences; go longer only when asked.
- No markdown, lists, emojis, headings, or URLs. Spell out numbers and symbols the way you would say them.
- The input is speech-to-text, so expect small errors; interpret generously and do not point them out.
- If you are unsure what was meant, ask one short clarifying question.
- The user can talk over you at any time; if a message looks like it cuts off your previous answer, just follow their new direction.
- You are NOT limited to talking. Your agent operates the user's computer for you: a real web browser (open sites, search, click, type, scroll, go back, read pages, look at the screen, use their logins like LinkedIn), their local files (resume, notes), web research and news. You can also run quick actions: open apps, folders and websites, set the volume, mute, lock, sleep. Never say you can't browse, open things, scroll or do tasks. If asked what you can do, say this.
- If the user wants something DONE on the computer and it reached you instead of your agent, reply with exactly <delegate/> and nothing else; it will be handed over.
- Questions about progress ("how's it going?", "what are you doing?") are answered from the status below, in plain words.
{agent_status}{facts}"""

# Any natural way of closing: "thanks", "that's all, thank you so much", "okay I'm done", "see you later Prometheus", "bye"...
# An utterance is a goodbye only if, after removing closing phrases and polite filler, NOTHING else is left ("thanks, now open
# LinkedIn" is a request, not a goodbye).
_CLOSERS = r"""\b(
    thanks?( you)?(\s+so|\s+very)?(\s+much|\s+a lot|\s+a ton)? | thank you | thx | ty | cheers | many thanks | much appreciated | i appreciate (it|that|you|this)
  | good ?bye | bye( bye)? | see (you|ya)(\s+later|\s+soon|\s+tomorrow|\s+around)? | (talk|speak) to you(\s+later|\s+soon)? | later | take care | good ?night | night
  | gotta go | have to go | i have to go | i'?ll let you go | peace( out)? | farewell | adios | ciao
  | (i'?m|i am|we'?re|we are|you'?re|you are|that'?s|that is|it'?s|it is|all) (all )?(done|finished|set|good|fine|it|all|everything|enough)
  | that'?ll be (all|it) | that'?s (all|it)( for (now|today))? | (that'?s )?(all|everything) i (need|needed|wanted|have) | nothing (else|more) | no more
  | (we'?re|i'?m|you'?re) done | i'?m good | we'?re good | all good | never ?mind | forget it
  | (please )?(stop|quit|exit|end|finish|close|dismiss|cancel)( (listening|the conversation|this|it|now|talking|conversation))*
  | go (to )?sleep | go away | that will be all | that'?s enough | enough
  | you (can|may) (stop|go|rest|leave)( now)? | (you'?re|you are) dismissed
)\b"""
CLOSERS = re.compile(re.sub(r"\s*\n\s*", "", re.sub(r"\s*\|\s*", "|", _CLOSERS)), re.I)
FILLER = {"ok", "okay", "alright", "all", "right", "great", "cool", "perfect", "awesome", "nice", "good", "fine", "sure", "yes", "yeah", "yep",
          "well", "so", "now", "for", "today", "now", "then", "and", "please", "prometheus", "hey", "hi", "hello", "just", "ill", "i", "i'll",
          "will", "thats", "that's", "it", "you", "your", "the", "a", "much", "very", "really", "help", "helping", "everything", "see", "um", "uh",
          "here", "bro", "buddy", "mate", "friend", "my", "thank"}
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Fixed phrases, synthesized at startup so they play instantly.
GREETINGS = ["Hi there, what can I do for you?", "Hey! I'm all ears.", "Hello! How can I help?", "I'm here. What do you need?",
             "Hi! What are we doing?", "Hey there, what's up?"]
_last_greeting = [""]


def greeting() -> str:
    """A friendly, varied hello when woken by name alone (never the same one twice in a row)."""
    import random
    pick = random.choice([g for g in GREETINGS if g != _last_greeting[0]])
    _last_greeting[0] = pick
    return pick


FAREWELLS = ["Take care! I'm here whenever you need me.", "Anytime! Just say my name when you need me.", "Sounds good. Talk to you soon!",
             "Alright, have a great day!", "See you later! Just call if you need anything.", "Bye for now! I'll be right here."]
THANKS_REPLIES = ["You're very welcome! Just say my name whenever you need me.", "Happy to help! Talk to you soon.",
                  "Anytime! Have a great day.", "My pleasure! I'm here if you need anything else."]
NIGHT_REPLIES = ["Good night! Sleep well.", "Good night! I'll be here in the morning."]
IDLE_FAREWELLS = ["I'll step back for now. Just say my name when you need me.", "I'll be right here if you need me. Just say Prometheus."]
_last_farewell = [""]


def farewell(heard: str = "") -> str:
    """A warm goodbye that fits what you said ("thanks" gets a "you're welcome"), never the same one twice running."""
    import random
    t = heard.lower()
    pool = (NIGHT_REPLIES if re.search(r"good ?night|\bnight\b", t) else THANKS_REPLIES if re.search(r"thank|thx|cheers|appreciate", t)
            else IDLE_FAREWELLS if not t else FAREWELLS)
    pick = random.choice([f for f in pool if f != _last_farewell[0]] or pool)
    _last_farewell[0] = pick
    return pick


PHRASES = ["Scrolling.", "Going back.", "Going forward.", "Reloading.", "New tab.", "Closing the tab.", "Paused.", "Playing.", "Muted.", "Unmuted.",
           "That's the bottom.", "That's the top.", *GREETINGS, *FAREWELLS, *THANKS_REPLIES, *NIGHT_REPLIES, *IDLE_FAREWELLS, "I'm listening.", "Sorry, something went wrong. Try again.",
           "Sorry, I didn't catch that.", "I keep running into errors, so I'll stop for now.", "Done.",
           "On it.", "Sure, on it.", "Got it, working on that.", "Okay, let me do that.", "Okay, stopped.", "Okay.",
           "Okay, I won't.", "Nothing is running right now."]


def is_goodbye(text: str) -> bool:
    t = re.sub(r"[^\w\s']", " ", text.lower()).strip()
    if not t or len(t.split()) > 12:
        return False
    rest, n = CLOSERS.subn(" ", t)
    return n > 0 and all(w in FILLER for w in rest.split())


def cfg_bool(config, key: str, default: bool) -> bool:
    return bool(config.get(key, default))


class DelegateRequested(Exception):
    """The chat model decided this needs the agent (it answered with <delegate/>)."""


FIRST_CLAUSE = re.compile(r"^(.{20,}?[,;:\u2014\u2013])\s")


async def sentences(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Group a token stream into sentences so speech can start early. The very first chunk is released at the first
    clause break (comma etc.) once it is long enough, since the first audio is what you are waiting for."""
    buf, first = "", True
    async for piece in stream:
        buf += piece
        parts = SENTENCE_END.split(buf)
        for s in parts[:-1]:
            if s.strip():
                yield s.strip()
                first = False
        buf = parts[-1]
        if first and (m := FIRST_CLAUSE.match(buf)) and len(m.group(1).split()) >= 4:
            yield m.group(1).strip()
            buf, first = buf[m.end():], False
        if len(buf) > 220 and "," in buf:            # long run-on: flush at the last comma
            head, _, tail = buf.rpartition(",")
            yield head.strip() + ","
            buf = tail
    if buf.strip():
        yield buf.strip()


class SentenceBuffer:
    """Starts consuming a sentence stream right away and lets you iterate it later. That lets a chat reply begin
    generating while the router is still deciding; if the reply isn't needed, cancel() throws it away."""

    def __init__(self, source: AsyncIterator[str], on_first=None) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        self._on_first = on_first
        self.task = asyncio.create_task(self._pump(source))

    async def _pump(self, source: AsyncIterator[str]) -> None:
        first = True
        try:
            async for s in source:
                if first and self._on_first:
                    self._on_first(s)
                first = False
                await self._q.put(s)
        except asyncio.CancelledError:
            raise
        except Exception as e:                      # surfaced to whoever iterates
            await self._q.put(e)
        finally:
            await self._q.put(None)

    def cancel(self) -> None:
        self.task.cancel()

    def __aiter__(self):
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        while True:
            item = await self._q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


@modes.register("conversation")
class ConversationMode(Mode):
    """Hands-free, full-duplex voice chat.

    Two independent halves run for the whole conversation:
      * the ears (core/ears.py): one mic stream that never closes, cutting what you say into utterances;
      * the mouth/brain: each reply is its own task (think, work, speak).
    The instant the ears hear you start talking, the current reply is cancelled (speech stops, the agent is
    interrupted) and what you say becomes the next request. Pressing the key does the same; pressing it while
    nothing is happening, or saying goodbye, ends the conversation."""
    name = "conversation"
    continuous = True

    def __init__(self, ctx: ModeContext) -> None:
        super().__init__(ctx)
        self.history: list[dict] = []
        self._turn: asyncio.Task | None = None
        self._errors = 0
        self._ending = False
        self._end = asyncio.Event()
        self._client = None
        self._last_state: str | None = None
        self.voice = VoiceChannel(ctx.config, ctx.bus, ctx.speaker, on_speech_start=self._on_speech_start,
                                  quiet=lambda: self.text.recently_used())     # hearing you
        self.text = TextChannel()                                                # typing to it
        self._user_talking = False                     # you are mid-sentence: don't talk over you with announcements
        self._t_end = self._t_text = 0.0               # latency marks (monotonic)
        self._tl: TurnTimeline | None = None

    def _state(self, state: str) -> None:
        """Publish a state change, skipping repeats (a flapping state makes the widget blink)."""
        if state != self._last_state:
            self._last_state = state
            self.ctx.bus.publish("state", state=state, mode=self.name)

    async def handle(self, text: str) -> None:        # Mode API; the loop lives in run()
        raise NotImplementedError

    async def respond(self, text: str) -> str:
        """Handle one utterance and speak the answer. Subclasses override this to route elsewhere."""
        return await self._reply_and_speak(sentences(self._tokens()))

    def holding(self) -> bool:
        """True while the conversation should stay open without hearing anything (music playing)."""
        return False

    async def filter_utterance(self, text: str) -> str | None:
        """Chance to ignore or rewrite what was heard before it becomes a turn (None = ignore silently)."""
        return text

    def set_typed_only(self, on: bool) -> None:
        """Typing mode is exclusive: while the type box is open nothing is heard; closing it brings your voice back."""
        self.text.exclusive = on
        self.voice.pause(on)

    def _on_speech_start(self) -> None:
        """You started talking: cut off whatever the assistant is doing, right now."""
        if self._ending:
            return
        if self.holding():                              # music: the "speech" is most likely the song; don't flicker or cut a reply
            return
        self._user_talking = True
        if self.keep_turn_on_speech():                # e.g. an approval question is waiting for your yes/no
            if self.ctx.speaker:
                self.ctx.speaker.stop()               # stop talking so we can hear you, but keep the task alive
            return
        self._cut_off()
        self._state("listening")

    def interrupt(self) -> bool:
        """Key press: cut the assistant off. True if it was busy; False means it was just waiting for you."""
        if self._busy():
            self._cut_off()
            return True
        return False

    def keep_turn_on_speech(self) -> bool:
        """True when hearing you must NOT cancel the running turn (it is waiting for your answer)."""
        return False

    async def intercept(self, text: str) -> bool:
        """Handle an utterance without starting a new turn (a spoken yes/no, "stop"...). True = consumed."""
        return False

    def agent_status(self) -> str:
        """What the conversational side knows about background work. The assistant mode fills this in."""
        return ""

    def _busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def _cut_off(self) -> None:
        if self._busy():
            self._turn.cancel()
        if self.ctx.speaker:
            self.ctx.speaker.stop()

    async def submit_text(self, text: str) -> None:
        """A typed message: it cuts off whatever is being said or done and becomes the newest instruction, like talking over it."""
        text = (text or "").strip()
        if not text:
            return
        self.text.note_typed()
        self._cut_off()
        self.ctx.bus.publish("transcript", text=text, mode=self.name)
        if is_goodbye(text):
            await self._goodbye(farewell(text))
            self._end.set()
            return
        if await self.intercept(text):
            return
        self._start_turn(text)

    # --- the loop ---
    async def run(self, mic, stt, initial: str = "") -> None:
        bus, cfg = self.ctx.bus, self.ctx.config
        self.history, self._turn, self._errors, self._ending = [], None, 0, False
        self.session_id = tel.new_session_id()                 # = the conversation's start time (epoch seconds): every trace carries it
        log.info("conversation session %s (started %s)", self.session_id, tel.session_label(self.session_id))
        self._end = asyncio.Event()
        no_speech_s = cfg.get("conversation.no_speech_s", 15.0)
        async def transcribe(audio) -> str:                    # runs speculatively inside the ears, before you've fully stopped
            return await asyncio.to_thread(stt.transcribe, audio, mic.sample_rate)
        await self.voice.decide_duplex()
        ears = self.voice.open(mic, transcribe, paused=self.text.exclusive)
        ears_task = asyncio.create_task(ears.run())
        idle_since, misses = time.monotonic(), 0
        try:
            self._last_state = None
            self._state("listening")
            if initial:                             # woken with a command attached: "Prometheus, open Safari"
                bus.publish("transcript", text=initial, mode=self.name)
                self._start_turn(initial)
            else:
                self._start_turn(None, ack=greeting() if cfg.get("wake_word.enabled", True) else "I'm listening.")
            while not self._end.is_set():
                try:
                    audio = await asyncio.wait_for(ears.next(), 0.4)
                except asyncio.TimeoutError:
                    if self._busy():
                        idle_since = time.monotonic()
                    elif self.holding() or self.text.exclusive:
                        idle_since = time.monotonic()          # music is playing, or you are typing: staying quiet is not 'nobody is here'
                    elif time.monotonic() - idle_since >= no_speech_s:
                        misses, idle_since = misses + 1, time.monotonic()
                        if misses >= 2:               # two quiet windows (~30 s) in a row: nobody is there
                            await self._goodbye(farewell())
                            return
                    continue
                idle_since = time.monotonic()
                self._user_talking = False                     # that utterance is finished
                utt_obj = audio if isinstance(audio, Utterance) else None
                text_task = audio.text_task if isinstance(audio, Utterance) else None
                self._t_end = audio.speech_end_at if isinstance(audio, Utterance) and audio.speech_end_at else time.monotonic()
                if isinstance(audio, Utterance):
                    audio = audio.audio
                if self.ctx.speaker:
                    self.ctx.speaker.wake()                    # wake the speakers while we think, so no lead-in delay later
                if not self.holding():
                    self._state("transcribing")
                try:
                    text = await (text_task if text_task else asyncio.to_thread(stt.transcribe, audio, mic.sample_rate))
                except Exception as e:
                    log.exception("transcription failed")
                    bus.publish("error", message=f"Transcription failed: {e}")
                    self._errors += 1
                    if self._errors >= 3:
                        break
                    self._state("listening")
                    continue
                if not text:
                    misses += 1
                    if misses >= 3:
                        await self._goodbye(farewell())
                        return
                    bus.publish("notice", text="Didn't catch that. Try again")
                    self._start_turn(None, ack="Sorry, I didn't catch that.")
                    continue
                gated = await self.filter_utterance(text) if text else text
                if gated is None:
                    continue                          # not addressed to us (e.g. lyrics while music plays): ignore, silently
                text = gated
                if self.voice.is_echo(text):
                    log.info("ignoring my own voice picked up by the mic: %r", text[:60])
                    if self.voice.words(text):                 # no English words = noise, not our voice: ignore it, don't blame the setup
                        self.voice.note_self_heard()
                    if getattr(utt_obj, "engaged", False):
                        self._on_false_barge()
                    self._state("listening" if not self._busy() else "thinking")
                    continue
                misses = 0
                self._t_text = time.monotonic()
                bus.publish("transcript", text=text, mode=self.name)
                if is_goodbye(text):
                    await self._goodbye(farewell(text))
                    return
                if await self.intercept(text):
                    continue
                self._start_turn(text)
            await self._goodbye("I keep running into errors, so I'll stop for now.")
        except asyncio.CancelledError:                  # ended by key/Esc: silence immediately
            self._ending = True
            self._cut_off()
            raise
        finally:
            ears_task.cancel()
            await asyncio.gather(ears_task, return_exceptions=True)     # releases the mic
            await self._drop_turn()

    async def _drop_turn(self) -> None:
        """Cancel the running reply, but never make the user wait for it to wind down (stopping an agent can take seconds)."""
        if self._busy():
            turn = self._turn
            turn.cancel()
            await asyncio.wait([turn], timeout=0.4)
            turn.add_done_callback(lambda t: t.cancelled() or t.exception())      # finishes in the background; its errors are not ours

    async def _goodbye(self, text: str) -> None:
        self._ending = True                             # the ears keep running, but must not cut the farewell off
        await self._drop_turn()
        await self._say(text)

    # --- one assistant turn, as its own task ---
    def _start_turn(self, text: str | None, ack: str | None = None, display: str | None = None) -> None:
        prev = self._turn
        self._turn = asyncio.create_task(self._run_turn(prev, text, ack, display))

    async def _run_turn(self, prev: asyncio.Task | None, text: str | None, ack: str | None, display: str | None = None) -> None:
        bus = self.ctx.bus
        if prev is not None and not prev.done():
            prev.cancel()
            await asyncio.gather(prev, return_exceptions=True)
        try:
            if ack is not None:
                await self._say(ack, display)
                return
            self._state("thinking")
            self.history.append({"role": "user", "content": text})
            self._tl = TurnTimeline(self._t_end, self._t_text, session_id=getattr(self, "session_id", ""))   # where this turn's time goes
            try:
                with tel.session_context(getattr(self, "session_id", "")):      # the agent's own spans join the same session
                    reply = await self.respond(text)
            except asyncio.CancelledError:
                self.history.append({"role": "assistant", "content": "(interrupted by the user)"})
                raise
            except Exception as e:                      # an API/tool error must not strand the user
                log.exception("respond failed")
                self.history.pop()                      # drop the unanswered turn so history stays alternating
                self._errors += 1
                bus.publish("error", message=f"{type(e).__name__}: {e}")
                if self._errors >= 3:
                    self._end.set()
                else:
                    await self._say("Sorry, something went wrong. Try again.")
                return
            self._errors = 0
            self._log_latency()
            self.history.append({"role": "assistant", "content": reply or "(done)"})
            max_turns = self.ctx.config.get("conversation.max_history_turns", 20)
            self.history = self.history[-2 * max_turns:]
        finally:
            # The mic never closed, so just say we're listening again, but only the newest turn announces it
            # (a turn cancelled by your voice is about to be replaced; announcing would make the widget flicker).
            if not self._ending and self._turn is asyncio.current_task():
                self._state("music" if self.holding() else "listening")

    def _log_latency(self) -> None:
        spk = self.ctx.speaker
        first = getattr(spk, "first_audio_at", 0.0) if spk else 0.0
        if (tl := getattr(self, "_tl", None)) is not None:
            tl.emit(first)
        if self._t_end and self._t_text and first > self._t_text:
            log.info("latency: stop talking -> text %.2fs | text -> voice %.2fs | total %.2fs",
                     self._t_text - self._t_end, first - self._t_text, first - self._t_end)

    async def _say(self, text: str, display: str | None = None) -> None:
        self.ctx.bus.publish("result", text=display or text)
        if self.ctx.speaker and self.ctx.config.get("tts.enabled", True):
            await self.ctx.speaker.speak(text)

    # --- the brain ---
    def _anthropic(self):
        if self._client is None:                                 # reuse one connection across turns
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        return self._client

    def _status_block(self) -> str:
        status = self.agent_status()
        return f"\nAgent status (for progress questions): {status}\n" if status else ""

    async def _tokens(self) -> AsyncIterator[str]:
        facts = Memory(DATA_DIR / "voicectl.db").facts()
        facts_txt = ("\nWhat you know about the user:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items())) if facts else ""
        async with self._anthropic().messages.stream(
            model=self.ctx.config.get("conversation.model", "claude-sonnet-4-6"),
            max_tokens=self.ctx.config.get("conversation.max_tokens", 400),
            system=SYSTEM.format(facts=facts_txt, agent_status=self._status_block()), messages=self.history,
        ) as stream:
            async for t in stream.text_stream:
                yield t

    def _start_chat(self) -> SentenceBuffer:
        """Begin generating the chat reply now. As soon as its first clause exists, its audio is synthesized too, so a
        reply confirmed as "chat" can start talking immediately."""
        spk = self.ctx.speaker
        return SentenceBuffer(sentences(self._tokens()), on_first=(spk.prefetch if spk else None))

    async def _reply_and_speak(self, source: AsyncIterator[str]) -> str:
        spoken: list[str] = []
        speaking = bool(self.ctx.speaker and self.ctx.config.get("tts.enabled", True))

        delegated = False

        async def tee() -> AsyncIterator[str]:
            nonlocal delegated
            async for sentence in source:
                if not spoken and sentence.lstrip().startswith("<delegate/>"):
                    delegated = True                                  # not for speaking: hand the request to the agent
                    return
                spoken.append(sentence)
                if not speaking:                                  # with TTS on, the Speaker reveals text in step with the voice
                    self.ctx.bus.publish("result", text=" ".join(spoken))
                yield sentence

        if speaking:
            await self.ctx.speaker.speak_many(tee(), show=True)
            if not delegated:
                self.ctx.bus.publish("result", text=" ".join(spoken))   # finished normally: show the whole reply
        else:
            async for _ in tee():
                pass
        if delegated:
            raise DelegateRequested()
        return " ".join(spoken)
