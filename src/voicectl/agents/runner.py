from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny,
    ResultMessage, StreamEvent, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, tool,
)

from voicectl.agents.browser import BrowserBackend, build_browser_server, default_browser
from voicectl.agents.risk import JevRisk
from voicectl.agents.screen import Screen, build_screen_server
from voicectl.core.config import ROOT, Config
from voicectl.core.events import EventBus
from voicectl.core.learnings import LearningStore
from voicectl.core.memory import Memory

log = logging.getLogger("voicectl.agent")

SAFE_TOOLS = [
    "Read", "Glob", "Grep", "Skill", "WebSearch", "WebFetch",
    "mcp__browser__browser_goto", "mcp__browser__browser_read", "mcp__browser__browser_press",
    "mcp__browser__browser_scroll", "mcp__browser__browser_back", "mcp__browser__browser_forward",
    "mcp__browser__browser_screenshot", "mcp__browser__browser_media", "mcp__browser__browser_tabs",
    "mcp__browser__browser_switch_tab", "mcp__browser__browser_new_tab", "mcp__browser__browser_close_tab",
    "mcp__screen__screen_look", "mcp__screen__screen_scroll", "mcp__screen__screen_move", "mcp__screen__screen_nudge",
    "mcp__prometheus__checklist_set", "mcp__prometheus__checklist_update", "mcp__prometheus__computer_action",
    "mcp__memory__remember", "mcp__memory__forget", "mcp__memory__list_facts",
    "mcp__memory__learn", "mcp__memory__list_learnings", "mcp__memory__forget_learning",
    "mcp__swarm__swarm_computers", "mcp__swarm__swarm_run", "mcp__swarm__swarm_wait", "mcp__swarm__swarm_status", "mcp__swarm__swarm_approve", "mcp__swarm__swarm_cancel",
]
RISKY_WORDS = re.compile(r"\b(apply|submit|send|buy|purchase|pay|order|delete|publish|checkout|check out|transfer|place bet|donate|withdraw)\b", re.I)
# Built-in tools that only read, search or keep notes: never worth interrupting you for.
QUIET_BUILTINS = {"ToolSearch", "Read", "Glob", "Grep", "Skill", "WebSearch", "WebFetch", "NotebookRead", "Task",
                  "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "BashOutput"}
# Read-only shell commands that are safe to run without asking.
SAFE_SHELL = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "egrep", "rg", "find", "echo", "date", "which", "whoami", "uname",
              "sw_vers", "mdfind", "mdls", "stat", "file", "du", "df", "sort", "uniq", "cut", "tr", "basename", "dirname",
              "pbpaste", "printenv", "env", "sleep", "true", "id", "hostname", "uptime", "ps", "defaults", "jq", "awk", "sed"}
_SHELL_META = re.compile(r"(>|<\(|`|\$\(|\brm\b|\bmv\b|\bcp\b|\bdd\b|\bsudo\b|\bkill\b|\bchmod\b|\bchown\b|-delete|-exec|\bwrite\b|\bdelete\b|\bsed\s+-i|\btee\b|\bcurl\b|\bwget\b|\bpip\b|\bbrew\b|\bnpm\b)")


def bash_is_readonly(command: str) -> bool:
    """True for pipelines of read-only commands with no redirects, substitutions or anything destructive."""
    if _SHELL_META.search(command) or "\n" in command.strip():
        return False
    segments = re.split(r"\|\||&&|\||;", command)
    heads = []
    for seg in segments:
        words = seg.strip().split()
        if not words:
            continue
        heads.append(words[0].rsplit("/", 1)[-1])
    return bool(heads) and all(h in SAFE_SHELL for h in heads) and not ("defaults" in heads and "write" in command)


BROWSER_WORKAROUND = re.compile(r"(https?://|\bosascript\b|google chrome|\bsafari\b|\bopen\s+-a\b|\bxdg-open\b|\bwebbrowser\b)", re.I)
RISK_CHECKED = {"browser_click", "browser_type", "screen_click", "screen_drag", "screen_key"}
RESET_PHRASES = {"new session", "start over", "forget that", "reset", "reset session", "start a new session"}
PLAN_RE = re.compile(r"<plan>(.*?)</plan>", re.S | re.I)


@dataclass
class TextStart:
    id: int                 # unique per text block within the session


@dataclass
class TextDelta:
    id: int
    text: str


@dataclass
class TextEnd:
    id: int


@dataclass
class Finished:
    text: str


class HiddenTagFilter:
    """Strips <thinking>/<plan>/... blocks out of streamed text, even when a tag is split across chunks.
    The model sometimes writes its reasoning in such tags; that must never be spoken or shown."""
    OPEN = re.compile(r"<(thinking|plan|reasoning|scratchpad|analysis)>", re.I)
    TAGS = ("<thinking>", "<plan>", "<reasoning>", "<scratchpad>", "<analysis>")

    def __init__(self) -> None:
        self.buf = ""
        self.hidden: str | None = None

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out: list[str] = []
        while True:
            if self.hidden is None:
                m = self.OPEN.search(self.buf)
                if m:
                    out.append(self.buf[:m.start()])
                    self.hidden, self.buf = m.group(1).lower(), self.buf[m.end():]
                    continue
                k = self.buf.rfind("<")                       # a tag might be starting at the very end: hold it back
                if k != -1 and any(t.startswith(self.buf[k:].lower()) for t in self.TAGS):
                    out.append(self.buf[:k])
                    self.buf = self.buf[k:]
                else:
                    out.append(self.buf)
                    self.buf = ""
                return "".join(out)
            close = f"</{self.hidden}>"
            i = self.buf.lower().find(close)
            if i != -1:
                self.hidden, self.buf = None, self.buf[i + len(close):]
                continue
            self.buf = self.buf[-(len(close) - 1):]            # keep only what could be the start of the closing tag
            return "".join(out)

    def flush(self) -> str:
        rest, self.buf = ("" if self.hidden else self.buf), ""
        return rest


HIDDEN_TOOLS = {"ToolSearch", "checklist_set", "checklist_update"}      # internal bookkeeping, not "steps" to show


def pretty_url(url: str) -> str:
    """"https://www.linkedin.com/jobs/search/?keywords=Applied%20AI..." -> "linkedin.com/jobs/search": readable, and no search terms on screen."""
    from urllib.parse import urlparse
    u = urlparse(url if "://" in url else f"https://{url}")
    path = "/".join(u.path.strip("/").split("/")[:2])
    return (u.netloc.removeprefix("www.") + (f"/{path}" if path else ""))[:48]


def describe_tool(name: str, inp: dict[str, Any]) -> str:
    short = name.split("__")[-1]
    match short:
        case "browser_goto": return f"Opening {pretty_url(str(inp.get('url', '')))}"
        case "browser_read": return "Reading the page"
        case "browser_click": return f"Clicking: {inp.get('description') or inp.get('index')}"
        case "browser_type": return f"Typing: {inp.get('description') or inp.get('text', '')[:40]}"
        case "browser_press": return f"Pressing {inp.get('key', '')}"
        case "browser_scroll": return f"Scrolling {inp.get('direction', 'down')}"
        case "browser_back": return "Going back"
        case "browser_forward": return "Going forward"
        case "browser_screenshot": return "Looking at the page"
        case "browser_media": return f"Video: {inp.get('action', '')}"
        case "swarm_computers": return "Checking your other computers"
        case "swarm_run": return f"Sending to {inp.get('computer') or 'the swarm'}: {str(inp.get('task', ''))[:50]}"
        case "swarm_wait": return "Waiting for the next computer to finish"
        case "swarm_status" | "swarm_approve" | "swarm_cancel": return "Checking the swarm"
        case "browser_tabs": return "Checking open tabs"
        case "browser_switch_tab": return f"Switching to the {inp.get('query', '')} tab"
        case "browser_new_tab": return f"Opening a new tab: {pretty_url(str(inp.get('url', '')))}"
        case "browser_close_tab": return "Closing a tab"
        case "screen_look": return "Looking at the screen"
        case "screen_click": return f"Clicking: {inp.get('description') or 'on the screen'}"
        case "screen_move": return "Moving the mouse"
        case "screen_nudge": return f"Moving the mouse {inp.get('direction', '')}"
        case "screen_scroll": return f"Scrolling {inp.get('direction', 'down')}"
        case "screen_drag": return f"Dragging: {inp.get('description') or ''}".strip()
        case "screen_type": return f"Typing: {inp.get('description') or inp.get('text', '')[:40]}"
        case "screen_key": return f"Pressing {inp.get('key', '')}"
        case "computer_action": return f"Doing: {inp.get('name', '').replace('_', ' ')}"
        case "chrome_setup": return "Turning on Chrome remote control"
        case "Read": return f"Reading {Path(inp.get('file_path', '')).name}"
        case "Glob": return f"Looking for files: {inp.get('pattern', '')}"
        case "Grep": return f"Searching files: {inp.get('pattern', '')}"
        case "WebSearch": return f"Searching the web: {inp.get('query', '')}"
        case "WebFetch": return f"Fetching {inp.get('url', '')}"
        case "Skill": return f"Using skill: {inp.get('skill') or inp.get('command', '')}"
        case "Bash": return f"Running: {str(inp.get('command', ''))[:70]}"
        case "Write" | "Edit": return f"Editing {Path(inp.get('file_path', '')).name}"
        case "remember": return f"Remembering: {inp.get('key', '')}"
        case "forget": return f"Forgetting: {inp.get('key', '')}"
    return short


def needs_approval(name: str, inp: dict[str, Any]) -> str | None:
    """Return the question to ask the user, or None if the step is safe to run unprompted."""
    short = name.split("__")[-1]
    if short == "chrome_setup":
        return "Okay to open Chrome's remote-control settings?"
    if short in {"browser_click", "browser_type", "screen_click", "screen_drag", "screen_type", "screen_key"}:
        typed = inp.get("text", "") if (short == "browser_type" and inp.get("submit")) else ""
        blob = f"{inp.get('description', '')} {typed}"
        return f"{describe_tool(name, inp)}?" if RISKY_WORDS.search(blob) else None
    if name in QUIET_BUILTINS or name.startswith("mcp__"):      # reading, searching, notes, our own safe tools
        return None
    if short == "Bash":
        return None if bash_is_readonly(str(inp.get("command", ""))) else f"Allow: {describe_tool(name, inp)}?"
    return f"Allow: {describe_tool(name, inp)}?"                 # writing/editing files and anything unrecognised


class AgentRunner:
    def __init__(self, config: Config, bus: EventBus, memory: Memory, browser: BrowserBackend | None = None,
                 screen: Screen | None = None, prompt_name: str = "agent_prompt.txt",
                 learnings: LearningStore | None = None, swarm_tools: bool = True) -> None:
        self.swarm_tools = swarm_tools      # False for an agent that is itself running a swarm task: it must not hand work back to the swarm
        self.config, self.bus, self.memory = config, bus, memory
        self.learnings = learnings or LearningStore.beside(memory)       # what it has learned about HOW to do things: its own store
        self.screen = screen
        self.prompt_name = prompt_name
        self.browser = browser or default_browser(config, screen)
        self.checklist: list[dict[str, str]] = []
        self.complexity = ""
        self.plan_summary = ""
        self.played_media = False          # did the agent press play/unmute during the current turn (an audio request was acted on)?
        self._block_ids = 0
        self.risk = JevRisk(config.get("agent.risk.threshold", 0.6)) if JevRisk.available() and config.get("agent.risk.enabled", True) else None
        self._client: ClaudeSDKClient | None = None
        self._session_row: int | None = None
        self._recap_session: int | None = None      # the previous conversation to recap into a fresh session
        self._approval: asyncio.Future[bool] | None = None
        self.timeline = None                 # a TurnTimeline for the current turn (set by the mode), or None
        self.mode_name = "agent"      # the mode the UI shows while this runs (Prometheus sets "assistant")
        self.task = ""                # what it is currently working on
        self.last_step = ""
        self.last_plan: list[str] = []

    @property
    def pending_approval(self) -> bool:
        return self._approval is not None and not self._approval.done()

    def status(self) -> str:
        """One line for the conversational side: what the agent is doing and where the browser is."""
        page = f" Browser is on: {self.browser.last_title or self.browser.last_url}." if self.browser.last_url else ""
        if self.task:
            step = f" Latest step: {self.last_step}." if self.last_step else ""
            if self.checklist:
                done = sum(1 for i in self.checklist if i["status"] == "done")
                step += f" Checklist: {done} of {len(self.checklist)} done."
            return f"Working on: {self.task}.{step}{page}"
        return f"Idle.{page}"

    # --- UI -> runner ---
    def on_ui_command(self, data: dict[str, Any]) -> None:
        if data.get("command") == "approval" and self._approval and not self._approval.done():
            self._approval.set_result(bool(data.get("approved")))

    # --- tools ---
    def _memory_server(self):
        mem = self.memory

        @tool("remember", "Save a durable fact (key + value) to long-term memory.", {"key": str, "value": str})
        async def remember(args):
            mem.remember(args["key"], args["value"])
            return {"content": [{"type": "text", "text": f"Saved {args['key']}"}]}

        @tool("forget", "Delete a saved fact by key.", {"key": str})
        async def forget(args):
            ok = mem.forget(args["key"])
            return {"content": [{"type": "text", "text": "Forgotten" if ok else "No such fact"}]}

        @tool("list_facts", "List all saved long-term facts.", {})
        async def list_facts(args):
            return {"content": [{"type": "text", "text": "\n".join(f"{k}: {v}" for k, v in mem.facts().items()) or "(none)"}]}

        @tool("learn", "Save a LESSON about how to do something well, for next time: a site quirk, a method that worked after another failed, "
                       "or a correction from the user. topic = the site or tool (e.g. 'linkedin'); lesson = one plain sentence. "
                       "Save it quietly; do not announce it.", {"topic": str, "lesson": str})
        async def learn(args):
            new = self.learnings.add_learning(args.get("topic", ""), args.get("lesson", ""), source="agent")
            return {"content": [{"type": "text", "text": "Saved the lesson." if new else "Already knew that (strengthened it)."}]}

        @tool("list_learnings", "List the lessons learned so far (id, topic, lesson). Use when the user asks what you have learned.", {})
        async def list_learnings(args):
            rows = self.learnings.learnings(40)
            return {"content": [{"type": "text", "text": "\n".join(f"{i}. [{t or 'general'}] {l}" for i, t, l, _ in rows) or "(nothing learned yet)"}]}

        @tool("forget_learning", "Delete lessons by id number or by a word in them.", {"query": str})
        async def forget_learning(args):
            n = self.learnings.forget_learning(str(args.get("query", "")))
            return {"content": [{"type": "text", "text": f"Forgot {n} lesson(s)." if n else "No lesson matched."}]}

        return create_sdk_mcp_server("memory", tools=[remember, forget, list_facts, learn, list_learnings, forget_learning])

    def _prometheus_server(self):
        runner = self

        def publish() -> None:
            runner.bus.publish("checklist", items=list(runner.checklist), summary=runner.plan_summary, level=runner.complexity)

        @tool("checklist_set", "Show the user the PLAN for a multi-step task as a checklist. summary: the goal in one sentence. "
                               "items: 3-8 short plain-language step names. Call this before starting a task that has several steps.",
              {"summary": str, "items": list})
        async def checklist_set(args):
            runner.plan_summary = str(args.get("summary") or "")[:140]
            runner.checklist = [{"text": str(t)[:80], "status": "pending"} for t in list(args.get("items") or [])[:8]]
            publish()
            if runner.complexity == "extended":        # a big plan: leave a beat for the user to read it / say "wait, change that"
                await asyncio.sleep(runner.config.get("agent.plan_pause_s", 2.0))
            return {"content": [{"type": "text", "text": f"Plan shown with {len(runner.checklist)} steps. Proceed."}]}

        @tool("checklist_update", "Update one checklist item (0-based index): status is 'in_progress', 'done' or 'skipped'. "
                                  "Mark a step in_progress when you start it and done when it is finished.",
              {"index": int, "status": str})
        async def checklist_update(args):
            i, st = int(args["index"]), str(args.get("status", "done"))
            if not (0 <= i < len(runner.checklist)):
                return {"content": [{"type": "text", "text": "No such checklist item."}], "is_error": True}
            runner.checklist[i]["status"] = st if st in ("in_progress", "done", "skipped", "pending") else "done"
            publish()
            return {"content": [{"type": "text", "text": "Updated."}]}

        @tool("computer_action", "Run a quick computer action. name is one of: open_app (app), open_url (url), open_folder "
                                 "(folder), search_web (query), set_volume (level 0-100), mute, lock_screen, sleep_mac. "
                                 "args is an object with that action's arguments.", {"name": str, "args": dict})
        async def computer_action(args):
            from voicectl import actions as actions_pkg
            from voicectl.actions.base import actions
            actions.discover(actions_pkg)
            try:
                cls = actions.get(args["name"])
                res = await asyncio.to_thread(lambda: cls().run(**{k: str(v) for k, v in (args.get("args") or {}).items()}))
            except KeyError as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            except TypeError as e:
                return {"content": [{"type": "text", "text": f"Wrong arguments: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": res.message}], **({} if res.ok else {"is_error": True})}

        @tool("chrome_setup", "Help the user switch on Chrome's remote control (opens the right page in Chrome, or toggles the setting), "
                              "which browser control needs once. Only call this after a browser tool reported SETUP NEEDED and the "
                              "user agreed.", {})
        async def chrome_setup(args):
            enable = getattr(runner.browser, "enable_apple_events", None)
            if enable is None:
                return {"content": [{"type": "text", "text": "This browser backend needs no setup."}]}
            try:
                return {"content": [{"type": "text", "text": await enable()}]}
            except Exception as e:
                runner.bus.publish("notice", text=str(e)[:200])
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}

        return create_sdk_mcp_server("prometheus", tools=[checklist_set, checklist_update, computer_action, chrome_setup])

    async def _can_use_tool(self, name: str, inp: dict[str, Any], ctx) -> PermissionResultAllow | PermissionResultDeny:
        if name == "Bash" and BROWSER_WORKAROUND.search(str(inp.get("command", ""))):
            # Shell commands can open a page but cannot click, type or play: the browser tools are the only honest way.
            return PermissionResultDeny(message="Don't drive the browser from the shell. Use the browser_* / screen_* tools. If "
                                                "they report SETUP NEEDED, tell the user exactly what to fix (or offer chrome_setup).")
        if not self.config.get("agent.require_approval", True):
            return PermissionResultAllow()
        question = needs_approval(name, inp)
        short = name.split("__")[-1]
        if question is None and self.risk and short in RISK_CHECKED and not (short == "browser_type" and not inp.get("submit")) \
                and not (short == "screen_key" and str(inp.get("key", "")).lower() not in ("return", "enter")):
            page = f"{self.browser.last_title} {self.browser.last_url}".strip()
            if await self.risk.is_risky(describe_tool(name, inp), page):          # the word list missed it; Jev didn't
                question = f"{describe_tool(name, inp)}?"
        if question is None:
            return PermissionResultAllow()
        self._approval = asyncio.get_running_loop().create_future()
        self.bus.publish("approval", text=question)
        try:
            ok = await asyncio.wait_for(self._approval, self.config.get("agent.approval_timeout_s", 120))
        except asyncio.TimeoutError:
            ok = False
        finally:
            self._approval = None
        self.bus.publish("state", state="acting", mode=self.mode_name)
        return PermissionResultAllow() if ok else PermissionResultDeny(message="The user declined this step. Stop and report.")

    # --- session lifecycle ---
    def _recap(self) -> str:
        """A compact memory of the previous conversation (what was asked, what was answered), never its raw thread: that is the part
        that is slow to reload. Empty after "new conversation", which deletes the turn log."""
        n = int(self.config.get("agent.recap_turns", 12))
        if n <= 0:
            return ""
        lines = [f"- You asked: {u[:200]} | I said: {a[:300]}" for u, a in self.memory.recent_turns(n)]
        return "\n".join(lines)

    def _system_prompt(self) -> str:
        base = (ROOT / "prompts" / self.prompt_name).read_text()
        paths = ", ".join(self.config.get("agent.context_paths", []))
        facts = "\n".join(f"- {k}: {v}" for k, v in self.memory.facts().items()) or "(none yet)"
        recap = self._recap()
        earlier = (f"\n\n<EarlierConversation>\nThis is what you and the user were doing before this fresh start. Use it if they refer back "
                   f"to it (\"what were we doing\", \"continue\"); do not recite it unprompted.\n{recap}\n</EarlierConversation>") if recap else ""
        return f"{base}\n\nLocal context folders: {paths}\n\n<Facts>\n{facts}\n</Facts>{earlier}"

    def _swarm_server(self) -> dict:
        """The tools for sending work to your other computers: only when the swarm is switched on, and never for an agent that is itself a
        swarm task (that would let work bounce between computers). Any problem here just means no swarm tools."""
        if not self.swarm_tools or not self.config.get("swarm.enabled", False):
            return {}
        try:
            from voicectl.swarm.tools import api_base, build_swarm_server
            from voicectl.swarm.wiring import swarm_token
            return {"swarm": build_swarm_server(api_base(self.config), swarm_token(), self.bus)}
        except Exception:
            log.warning("swarm tools unavailable", exc_info=True)
            return {}

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        dirs = [str(Path(p).expanduser()) for p in self.config.get("agent.context_paths", [])]
        return ClaudeAgentOptions(
            system_prompt=self._system_prompt(),
            model=self.config.get("agent.model", "claude-sonnet-4-6"),
            cwd=str(ROOT), add_dirs=dirs, setting_sources=["project"], skills="all",
            mcp_servers={"browser": build_browser_server(self.browser), "memory": self._memory_server(),
                         "prometheus": self._prometheus_server(),
                         **({"screen": build_screen_server(self.screen)} if self.screen else {}), **self._swarm_server()},
            allowed_tools=SAFE_TOOLS, can_use_tool=self._can_use_tool,
            permission_mode="default", resume=resume,
            include_partial_messages=True,                       # stream words so they can be spoken as they are written
            **({} if self.config.get("agent.thinking", False) else {"thinking": {"type": "disabled"}}),   # faster first word
        )

    async def end_session(self) -> None:
        last = self.memory.latest_session()
        self._recap_session = last["id"] if last else None      # the next session recaps this one (empty if its turns were deleted)
        self.memory.end_sessions()
        if self._client:
            await self._client.disconnect()
        self._client, self._session_row = None, None

    ACTION_TOOLS = {"browser_click", "browser_type", "browser_goto", "browser_new_tab", "screen_click", "browser_switch_tab"}

    def _action_record(self, tool: str, inp: dict, ok: bool) -> tuple[str, str, str, bool] | None:
        """(host, tool, key, ok) for an action worth learning from; None for reads, scrolls and the like."""
        if tool not in self.ACTION_TOOLS:
            return None
        from urllib.parse import urlparse
        if tool in ("browser_goto", "browser_new_tab"):
            u = urlparse(inp.get("url", "") if "://" in str(inp.get("url", "")) else f"https://{inp.get('url', '')}")
            return (u.netloc, tool, "/" + "/".join(u.path.strip("/").split("/")[:2]), ok)
        host = urlparse(self.browser.last_url or "").netloc
        key = str(inp.get("description") or inp.get("query") or "")
        return (host, tool, key, ok) if key else None

    def _learn_recoveries(self, records: list[tuple[str, str, str, bool]], limit: int = 2) -> None:
        """A failure followed, on the same site in the same task, by a different action that worked: remember the pair."""
        saved = 0
        for i, (host, tool, key, ok) in enumerate(records):
            if ok or saved >= limit:
                continue
            nxt = next((r for r in records[i + 1:] if r[3] and r[0] == host and (r[1], r[2]) != (tool, key)), None)
            if nxt and self.learnings.record_recovery(host, (tool, key), (nxt[1], nxt[2])):
                saved += 1

    async def _compact(self, row: dict) -> None:
        """Condense a long conversation: older exchanges become a short summary note (a fact, so it survives), the last few stay as the
        recap, and the next turn starts a fresh agent thread with none of the tool output (page text, screenshots) of the old one."""
        keep = int(self.config.get("agent.recap_turns", 12))
        turns = self.memory.recent_turns(200)
        older = turns[:-keep] if len(turns) > keep else []
        if older:
            text = "\n".join(f"User: {u[:200]}\nAssistant: {a[:300]}" for u, a in older)
            try:
                from voicectl.core.maintenance import DIGEST_PROMPT, summarize
                old_note = self.memory.facts().get("session_digest", "")
                digest = (await asyncio.wait_for(summarize(((old_note + "\n") if old_note else "") + text), 20)).strip()
            except Exception as e:
                log.info("compaction summary unavailable (%s); keeping the recap only", e)
                digest = ""
            if digest:
                self.memory.remember("session_digest", digest[:900])
        log.info("compacting the conversation after %s turns", row["turns"])
        self.bus.publish("notice", text="Tidying our conversation so I stay quick.")
        await self.end_session()

    async def _ensure_session(self) -> tuple[ClaudeSDKClient, int]:
        idle = self.config.get("agent.idle_timeout_min", 30) * 60
        row = self.memory.active_session(idle)
        if row is not None and self._client is not None and row["turns"] >= self.config.get("agent.compact_after_turns", 20):
            await self._compact(row)                 # a long thread drags every page read and screenshot along: condense it
            row = self.memory.active_session(idle)
        if row is None:
            await self.end_session()
            self._session_row = self.memory.new_session()
            self._client = ClaudeSDKClient(self._options(resume=None))
            await self._client.connect()
        elif self._client is None or self._session_row != row["id"]:
            if self.config.get("agent.resume_after_restart", False):
                self._session_row = row["id"]        # daemon restarted: resume the stored Claude session
                self._client = ClaudeSDKClient(self._options(resume=row["claude_session_id"]))
            else:                                    # resuming a long thread re-reads all of it: slow and costly. Start clean with a recap
                self._recap_session = row["id"]
                self.memory.end_sessions()
                self._session_row = self.memory.new_session()
                self._client = ClaudeSDKClient(self._options(resume=None))
            await self._client.connect()
        return self._client, self._session_row

    def _publish_session(self) -> None:
        row = self.memory.active_session(1e12)
        if row:
            mins = int((time.time() - row["started"]) / 60)
            self.bus.publish("session", label=f"SESSION · TURN {row['turns'] + 1} · {mins} MIN")

    # --- one turn ---
    async def run_turn(self, text: str, context: str = "") -> str:
        """Run a turn to completion and return the final text (for callers that don't care about streaming)."""
        final = ""
        async for ev in self.stream_turn(text, context):
            if isinstance(ev, Finished):
                final = ev.text
        return final

    async def stream_turn(self, text: str, context: str = "", hints: str = "", complexity: str = "",
                          publish_result: bool = True, _retried: bool = False) -> AsyncIterator[TextStart | TextDelta | TextEnd | Finished]:
        """One user request. Yields the model's words as they stream (so they can be spoken at once) and publishes
        tool steps, the checklist and cost on the event bus as they happen. `publish_result=False`: the caller shows the
        final answer itself, AFTER it has finished speaking (announcing it here would flash the whole text on screen while
        the voice is still reading the earlier part)."""
        if text.lower().strip(" .!?") in RESET_PHRASES:
            await self.end_session()
            self.bus.publish("result", text="Started a fresh session.")
            yield Finished("Started a fresh session.")
            return
        self.task, self.last_step, self.last_plan, self.checklist = text, "", [], []
        self.complexity, self.plan_summary, self.played_media = complexity, "", False
        self.bus.publish("checklist", items=[], summary="", level=complexity)
        try:
            client, row_id = await self._ensure_session()
            self._publish_session()
            self.bus.publish("state", state="thinking", mode=self.mode_name)
            prompt = text
            lessons = self.learnings.relevant_learnings(f"{text} {self.browser.last_url}")
            if lessons:                                          # what experience says about THIS kind of request
                hints = (hints + "\n" if hints else "") + "<Lessons>\n" + "\n".join(f"- {l}" for l in lessons) + "\n</Lessons>"
            if context or hints:      # context: what was just said / done elsewhere; hints: how much planning to show
                prompt = (f"{hints}\n" if hints else "") + (f"<ConversationContext>\n{context}\n</ConversationContext>\n" if context else "") \
                         + f"\nRequest: {text}"
            await client.query(prompt)
            if self.timeline:
                self.timeline.mark("query_sent")

            steps: dict[str, str] = {}
            open_blocks: dict[tuple[int, int], int] = {}        # (message number, block index) -> text block id
            filters: dict[int, HiddenTagFilter] = {}
            msg_no, streamed_text, final, claude_sid = 0, False, "", None
            tool_calls, capped = 0, False
            uses: dict[str, tuple[str, dict]] = {}              # tool_use id -> (tool, input), to pair with its result
            records: list[tuple[str, str, str, bool]] = []      # (host, tool, key, ok) in order, for this turn
            outcome: list[str] = []                              # "Opening X" / "FAILED: Clicking Y": what the long-term memory learns from
            cap = int(self.config.get("agent.single_step_tool_cap", 6)) if complexity == "single" else \
                int(self.config.get("agent.task_tool_cap", 40)) if complexity in ("multi", "extended") else 0
            responses = client.receive_response().__aiter__()
            first_s = self.config.get("agent.first_response_timeout_s", 40)
            stuck = False
            while True:
                try:                                                   # the first sign of life must come quickly; else the thread is stuck
                    msg = await (asyncio.wait_for(responses.__anext__(), first_s) if msg_no == 0 and not streamed_text else responses.__anext__())
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    stuck = True
                    break
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id:                   # a sub-agent's chatter is not for the user
                        continue
                    e, kind = msg.event, msg.event.get("type")
                    if kind == "message_start":
                        msg_no += 1
                        if self.timeline:
                            self.timeline.mark("model_reply_started")
                    elif kind == "content_block_start" and e["content_block"].get("type") == "text":
                        self._block_ids += 1
                        open_blocks[(msg_no, e["index"])] = self._block_ids
                        filters[self._block_ids] = HiddenTagFilter()
                        streamed_text = True
                        yield TextStart(self._block_ids)
                    elif kind == "content_block_delta" and e["delta"].get("type") == "text_delta":
                        if (bid := open_blocks.get((msg_no, e["index"]))) is not None:
                            if visible := filters[bid].feed(e["delta"]["text"]):
                                yield TextDelta(bid, visible)
                    elif kind == "content_block_stop" and (bid := open_blocks.pop((msg_no, e["index"]), None)) is not None:
                        if tail := filters.pop(bid).flush():
                            yield TextDelta(bid, tail)
                        yield TextEnd(bid)
                elif isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and not streamed_text and b.text.strip():     # no stream events: speak the whole block
                            flt = HiddenTagFilter()
                            visible = flt.feed(b.text) + flt.flush()
                            self._block_ids += 1
                            yield TextStart(self._block_ids)
                            if visible.strip():
                                yield TextDelta(self._block_ids, visible)
                            yield TextEnd(self._block_ids)
                        elif isinstance(b, ToolUseBlock):
                            if b.name.split("__")[-1] in HIDDEN_TOOLS:
                                continue
                            if b.name.split("__")[-1] == "browser_media" and (b.input or {}).get("action") in ("play", "toggle", "restart", "unmute"):
                                self.played_media = True
                            tool_calls += 1
                            uses[b.id] = (b.name.split("__")[-1], dict(b.input or {}))
                            if self.timeline:
                                self.timeline.tool_start(b.id, b.name.split("__")[-1])
                            if cap and tool_calls > cap:                 # a one-step request that keeps going: stop it
                                capped = True
                                break
                            desc = describe_tool(b.name, b.input or {})
                            steps[b.id] = desc
                            self.last_step = desc
                            self.bus.publish("state", state="acting", mode=self.mode_name)
                            self.bus.publish("step", text=desc, done=False)
                    streamed_text = False
                    if capped:
                        break
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock) and b.tool_use_id in steps:
                            self.bus.publish("step", text=steps[b.tool_use_id], done=True, failed=bool(b.is_error))
                            outcome.append(("FAILED: " if b.is_error else "") + steps[b.tool_use_id][:70])
                            if self.timeline:
                                self.timeline.tool_end(b.tool_use_id, not b.is_error)
                            if (rec := self._action_record(*uses.get(b.tool_use_id, ("", {})), ok=not b.is_error)):
                                records.append(rec)
                                self.learnings.record_action(*rec[:3], ok=rec[3])
                elif isinstance(msg, ResultMessage):
                    final, claude_sid = msg.result or "", msg.session_id
                    self.bus.publish("cost", usd=msg.total_cost_usd or 0.0, turns=msg.num_turns)
                    if msg.is_error:
                        self.bus.publish("error", message=final or "Agent error")

            if capped:
                log.warning("single-step request used more than %s tool calls; stopping it", cap)
                await self.interrupt()
                self.bus.publish("notice", text="That was a one-step request, so I stopped there." if complexity == "single"
                                 else "That task ran long, so I stopped it. Tell me where to pick up.")

            if stuck:                                                  # no sign of life: drop this thread and answer on a clean one
                log.warning("agent gave no response in %ss; starting a clean session%s", first_s, "" if _retried else " and retrying")
                try:
                    await asyncio.wait_for(client.interrupt(), 3)
                except Exception:
                    pass
                await self.end_session()
                if _retried:
                    msg_ = "I got stuck and couldn't answer. I've started a new conversation, so please try again."
                    self.bus.publish("error", message=msg_)
                    yield Finished(msg_)
                    return
                self.bus.publish("notice", text="That was taking too long. Starting a clean conversation and trying again.")
                async for ev in self.stream_turn(text, context, hints, complexity, publish_result, _retried=True):
                    yield ev
                return

            flt = HiddenTagFilter()
            final = (flt.feed(PLAN_RE.sub("", final)) + flt.flush()).strip()
            self._learn_recoveries(records)
            self.memory.record_turn(row_id, claude_sid)
            self.memory.log_turn(row_id, text, final, " | ".join(outcome[-8:]))
            if self.checklist:                                         # the work is over: the plan must not linger on screen
                self.bus.publish("checklist", items=[], summary="", level="none")
                self.checklist = []
            if publish_result:
                self.bus.publish("result", text=final)
            yield Finished(final)
        finally:
            self.task = ""

    async def warm_up(self) -> None:
        """Start the agent process now (it takes a few seconds), so the first agent task isn't slow."""
        try:
            await self._ensure_session()
        except Exception as e:
            log.warning("agent warm-up failed: %s", e)
        # The browser bridge is NOT started here: every new connection makes Chrome show an "Allow" dialog, so it starts
        # on the first browser action instead (and then stays connected for the whole session).

    async def interrupt(self) -> None:
        """Stop the in-flight turn (the user talked over it, or ended the conversation mid-task)."""
        if self._client:
            try:
                await self._client.interrupt()
                async def drain() -> None:          # swallow the interrupted turn's leftovers so the next turn starts clean
                    async for _ in self._client.receive_response():
                        pass
                await asyncio.wait_for(drain(), 8)
            except Exception as e:
                log.warning("interrupt failed: %s", e)
        if self._approval and not self._approval.done():
            self._approval.set_result(False)

    async def close(self) -> None:
        if self._client:
            await self._client.disconnect()
        await self.browser.close()
