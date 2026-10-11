from __future__ import annotations

"""Hourly memory hygiene for Prometheus, run only when it is idle (never mid-conversation or mid-task).

What "cleaning memory" means here:
  1. Conversation memory: a session older than `memory.session_max_age_min` is distilled into a short "last session" note
     (kept in long-term facts) and closed, so the next request starts with a fresh, small context instead of an ever
     growing one, but still knows what mattered.
  2. Housekeeping: old turn logs, extra facts, oversized log files and the voice cache are pruned.
  3. Process memory: garbage-collect; if the background process has grown past `memory.max_rss_mb`, restart it (the app
     relaunches it within seconds).
Saved facts you asked it to remember (resume summary, preferences...) are never deleted by this.
"""

import asyncio
import gc
import json
import logging
import re
import os
from voicectl import llm, platform
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from voicectl.core.config import DATA_DIR, Config
from voicectl.core.events import EventBus
from voicectl.core.learnings import LearningStore
from voicectl.core.memory import Memory

log = logging.getLogger("voicectl.maintenance")

PROTECTED_FACTS = {"resume_summary", "session_digest"}
DIGEST_PROMPT = ("Below is a log of what a voice assistant and its user did in the last session. Write a note of at most 120 words "
                 "that the assistant can read next time: the user's goals and preferences that came up, what was found or decided, "
                 "and anything left unfinished. Facts only, no chit-chat, no greetings.\n\n{log}")


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    restart: bool = False

    def add(self, text: str) -> None:
        self.lines.append(text)

    def __str__(self) -> str:
        return "; ".join(self.lines) or "nothing to clean"


def rss_mb() -> float:
    """This process's resident memory in MB."""
    return platform.current().process_rss_kb(os.getpid()) / 1024


async def summarize(text: str) -> str:
    """One short call to a small model. Raises if unavailable (the caller falls back to a plain digest)."""
    return await asyncio.to_thread(lambda: llm.current().text(model="claude-haiku-4-5", max_tokens=300, prompt=DIGEST_PROMPT.format(log=text)))


EXTRACT_PROMPT = """You maintain the long-term memory of a voice assistant. Below is a log of recent exchanges: what the user asked, what the
assistant answered, and the steps it took (steps marked FAILED did not work). Return ONLY JSON:
{"facts": [{"key": "snake_case_key", "value": "short value"}], "learnings": [{"topic": "site or tool", "lesson": "one sentence", "kind": "tip|pitfall|preference"}]}
facts = durable things about the USER (goals, target roles or companies, preferences, tools they use, how they like things done). Not one-off requests.
learnings = reusable lessons about HOW TO DO things well, from what worked or failed (a site quirk, a better method after a failure, a correction the
user gave). Not a description of what happened. Be selective: return empty lists if there is nothing durable. At most 5 of each, no secrets.

LOG:
{log}"""


async def extract_memory(text: str) -> dict:
    """One call to a small model: durable facts about the user and lessons learned. Raises if unavailable."""
    def go() -> dict:
        raw = llm.current().text(model="claude-haiku-4-5", max_tokens=700, prompt=EXTRACT_PROMPT.replace("{log}", text))
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}
    return await asyncio.to_thread(go)


class Maintenance:
    def __init__(self, config: Config, bus: EventBus, memory: Memory, is_idle: Callable[[], bool],
                 get_runner: Callable[[], object | None], summarizer: Callable[[str], Awaitable[str]] = summarize,
                 rss: Callable[[], float] = rss_mb, data_dir: Path | None = None,
                 extractor: Callable[[str], Awaitable[dict]] = extract_memory, learnings: LearningStore | None = None) -> None:
        self.extractor = extractor
        self.learnings = learnings or LearningStore.beside(memory)
        self.config, self.bus, self.memory = config, bus, memory
        self.is_idle, self.get_runner, self.summarize, self.rss = is_idle, get_runner, summarizer, rss
        self.data_dir = data_dir or DATA_DIR
        self._task: asyncio.Task | None = None
        self.on_restart: Callable[[], None] = lambda: None

    async def learn_from_recent(self, min_turns: int = 3) -> str:
        """Turn recent exchanges into long-term memory: durable facts about the user and lessons about how to do things. Runs before
        old turns are pruned or deleted, so what mattered outlives the conversation that produced it. Never learns from raw speech
        alone: the log pairs each request with the answer and the steps that did or did not work."""
        cursor = int(self.memory.get_meta("extracted_turn_id", "0") or 0)
        newest = self.memory.newest_turn_id()
        if cursor > newest:                                    # every turn was pruned and numbering restarted
            cursor = 0
        turns = self.memory.turns_after(cursor)
        if len(turns) < min_turns:
            return ""
        log_text = "\n\n".join(f"User: {u[:300]}\nAssistant: {a[:300]}\nSteps: {st or '(none)'}" for _, u, a, st in turns)
        try:
            found = await asyncio.wait_for(self.extractor(log_text), 40)
        except Exception as e:
            log.info("memory extraction unavailable (%s); will try again later", e)
            return ""
        facts_added = learn_added = 0
        existing = self.memory.facts()
        for f in (found.get("facts") or [])[:5]:
            key, value = re.sub(r"\W+", "_", str(f.get("key", "")).strip().lower()).strip("_")[:40], str(f.get("value", "")).strip()[:200]
            if key and value and key not in PROTECTED_FACTS and existing.get(key) != value:
                self.memory.remember(key, value)
                facts_added += 1
        for l in (found.get("learnings") or [])[:5]:
            if self.learnings.add_learning(str(l.get("topic", "")), str(l.get("lesson", "")), str(l.get("kind", "tip")) or "tip"):
                learn_added += 1
        self.memory.set_meta("extracted_turn_id", str(turns[-1][0]))
        self.learnings.prune_learnings(self.config.get("memory.max_learnings", 300))
        return f"learned {facts_added} fact(s) and {learn_added} lesson(s)"

    async def forget_learnings(self) -> str:
        """Erase what it has learned about HOW to do things (separate from forgetting you and your conversations)."""
        self.learnings.clear()
        self.bus.publish("notice", text="Forgot everything I'd learned about how to do things.")
        return "learnings cleared"

    async def new_conversation(self, forget_hook: Callable[[], None] = lambda: None) -> str:
        """A clean context window with the same Prometheus: the running agent conversation is closed, the chat history and the
        carried-over "last session" note are dropped, so nothing from the old (possibly stuck) thread leaks in. What it knows about
        you (facts, preferences, resume summary) stays."""
        runner = self.get_runner()
        if runner is not None:
            try:
                await asyncio.wait_for(runner.interrupt(), 3)
            except Exception:
                pass
            try:
                await asyncio.wait_for(runner.end_session(), 5)
            except Exception:
                log.warning("couldn't close the agent session cleanly", exc_info=True)
        await self.learn_from_recent(min_turns=1)             # keep what was learned; only the conversation itself is dropped
        self.memory.forget("session_digest")
        self.memory.clear_turns()
        forget_hook()
        self.bus.publish("session", label="")
        self.bus.publish("notice", text="New conversation. Clean slate; I still know your preferences.")
        return "new conversation"

    async def start_fresh(self, forget_hook: Callable[[], None] = lambda: None) -> str:
        """Forget EVERYTHING: the running conversation, every stored fact, session and turn. Unlike the hourly cleanup (which
        keeps what matters), this is a clean slate."""
        runner = self.get_runner()
        if runner is not None:
            try:
                await asyncio.wait_for(runner.interrupt(), 3)
            except Exception:
                pass
            try:
                await asyncio.wait_for(runner.end_session(), 5)
            except Exception:
                log.warning("couldn't close the agent session cleanly", exc_info=True)
        self.memory.clear()
        forget_hook()
        self.bus.publish("session", label="")
        self.bus.publish("notice", text="Memory cleared. Starting fresh.")
        return "memory cleared"

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        every = self.config.get("memory.cleanup_every_min", 60) * 60
        while True:
            await asyncio.sleep(every)
            try:
                report = await self.clean_when_idle()
                log.info("hourly memory cleanup: %s", report)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory cleanup failed")

    async def clean_when_idle(self, max_wait_s: float = 1800, poll_s: float = 15) -> Report:
        """Wait (up to 30 min) for a quiet moment: never clean in the middle of a conversation or a task."""
        waited = 0.0
        while not self.is_idle() and waited < max_wait_s:
            await asyncio.sleep(poll_s)
            waited += poll_s
        if not self.is_idle():
            r = Report()
            r.add("skipped: busy for the whole window")
            return r
        return await self.clean()

    async def clean(self, force: bool = False) -> Report:
        cfg, r = self.config, Report()
        if learned := await self.learn_from_recent(min_turns=1 if force else 3):
            r.add(learned)
        await self._conversation_memory(r, force)
        pruned = self.memory.prune_turns(cfg.get("memory.keep_turns_hours", 24) * 3600)
        self.learnings.prune_actions()
        dropped = self.memory.prune_facts(cfg.get("memory.max_facts", 200), PROTECTED_FACTS)
        if pruned or dropped:
            r.add(f"pruned {pruned} old turn(s), {dropped} extra fact(s)")
        trimmed = self._trim_files()
        if trimmed:
            r.add(f"trimmed {trimmed}")
        before = self.rss()
        gc.collect()
        after = self.rss()
        r.add(f"process memory {after:.0f} MB")
        if after > cfg.get("memory.max_rss_mb", 3500) and self.is_idle():
            r.add("over the limit: restarting")
            r.restart = True
            self.on_restart()
        return r

    # ---- pieces ----
    async def _conversation_memory(self, r: Report, force: bool) -> None:
        sess = self.memory.latest_session()
        if not sess or sess["last_used"] <= 0:
            return                                          # no live session
        age_min = (time.time() - sess["started"]) / 60
        if not force and age_min < self.config.get("memory.session_max_age_min", 60):
            return
        turns = self.memory.turns_for(sess["id"])
        if turns:
            log_text = "\n".join(f"User: {u}\nAssistant: {a}" for u, a in turns)
            try:
                digest = (await self.summarize(log_text)).strip()
            except Exception as e:                           # offline: a plain digest is better than losing the thread
                log.info("digest via model unavailable (%s); using plain digest", e)
                digest = " | ".join(f"{u[:70]} -> {a[:90]}" for u, a in turns[-6:])
            if digest:
                self.memory.remember("session_digest", digest[:900])
        runner = self.get_runner()
        if runner is not None:
            await runner.end_session()                      # fresh context next time (also marks the session ended)
        else:
            self.memory.end_sessions()
        r.add(f"closed a {age_min:.0f}-minute session ({len(turns)} turn(s)) and saved a short note")

    def _trim_files(self) -> str:
        limit = self.config.get("memory.max_log_mb", 5) * 1024 * 1024
        trimmed = []
        for f in self.data_dir.glob("*.log"):
            try:
                if f.stat().st_size > limit:
                    keep = f.read_bytes()[-1024 * 1024:]     # keep the last megabyte
                    f.write_bytes(keep)
                    trimmed.append(f.name)
            except OSError:
                pass
        cache = self.data_dir / "tts_cache"
        if cache.is_dir():
            files = sorted(cache.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            for f in files[:-300]:
                f.unlink(missing_ok=True)
                f.with_suffix(".json").unlink(missing_ok=True)
            if len(files) > 300:
                trimmed.append(f"{len(files) - 300} cached voice clips")
        return ", ".join(trimmed)
