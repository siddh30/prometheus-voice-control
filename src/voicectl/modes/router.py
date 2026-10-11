from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from voicectl import llm
from voicectl.actions.base import Action, actions
from voicectl.actions.router import match_pattern
from voicectl.core.config import Config

log = logging.getLogger("voicectl.router")

ROUTER_SYSTEM = """You route one spoken request to Prometheus, a voice assistant that can really DO things on the user's computer. It has hands:
- a real web browser it controls: open any site, search, click, type, fill forms, SCROLL, go back/forward, read pages, look at the screen, use the user's logins (LinkedIn etc.);
- the user's local files (resume, notes) and web research and news;
- simple computer actions: open an app, folder or website, search Google, set the volume, mute, lock, sleep.

Pick exactly one tool:
- an action tool: ONE simple, self-contained computer command (open Safari, open the downloads folder, volume 30, sleep).
- agent_task: anything involving the browser or a page ("open LinkedIn and search...", "scroll down", "click the second one", "go back", "read that to me", "what's on the page/screen", "apply filters"), files or the resume, job search, research, news, comparing or analysing things. Short follow-ups that refer to what is on screen or what the agent just did ("scroll down", "next page", "open that one", "do it again", "now for Google") are agent_task. When in doubt between a simple action and real work, choose agent_task.
- control: ONLY when a task is running and the user wants to stop, cancel, pause or abandon it ("stop", "cancel that", "never mind", "forget it").
- chat: conversation, general knowledge, opinions, advice, small talk, questions about you, or questions about the task's progress ("how's it going?", "what are you doing?"). Never choose chat for something the user wants DONE.

The input is speech-to-text, so it may contain errors. Use the recent exchange to resolve follow-ups. Call exactly one tool."""

ROUTE_QUESTION = (
    "Which kind of request is this? action: one simple computer command (open an app, folder or website, search "
    "the web, sleep, lock, volume, mute). agent: anything involving the browser or a web page (open, search, click, type, "
    "scroll, go back, read, look at the screen) or the user's files/resume, job search, research, news, comparing options, "
    "and short follow-ups about what is on screen (scroll down, next page, open that one). control: stop/cancel/never mind "
    "for a task that is running. chat: conversation, general knowledge, advice, opinions, small talk, or progress questions "
    "(how is it going). Never choose chat for something the user wants done. If it could be a simple command or real work, "
    "choose agent."
)
FAST_PATH_MAX_WORDS = 8          # short + a clean regex hit = run instantly, no model call

_LEAD = re.compile(r"^(?:(?:hey|okay|ok|so|um|uh|well|and|then|right|alright)[,\s]+)*"
                   r"(?:(?:can|could|would|will) you(?: please)?|please|i(?:'d| would) like you to|i want you to|go ahead and|let's)\s+", re.I)
_TAIL = re.compile(r"[,\s]*(?:for me|please|thanks|thank you|now|right now)\W*$", re.I)


def normalize(text: str) -> str:
    """Strip politeness so "Can you open LinkedIn for me, please?" becomes "open LinkedIn" for the instant path."""
    t = text.strip()
    for _ in range(2):
        t = _TAIL.sub("", _LEAD.sub("", t)).strip()
    return t


@dataclass
class JevRead:
    """Everything one Jev call says about an utterance."""
    route: str
    route_conf: float
    action: str | None
    action_conf: float
    complexity: str | None
    complexity_conf: float


@dataclass
class Route:
    kind: str                                    # "action" | "agent" | "chat"
    action: type[Action] | None = None
    args: dict[str, str] = field(default_factory=dict)
    how: str = "llm"                             # "pattern" | "jev" | "llm"
    confidence: float | None = None


def _llm_tools() -> list[dict]:
    tools = [c.spec() for c in actions.all().values()]
    tools.append({"name": "control", "description": "Stop, cancel or abandon the task that is currently running.",
                  "input_schema": {"type": "object", "properties": {}}})
    tools.append({"name": "agent_task", "description": "Multi-step work: browsing websites, files/resume analysis, research, job search, news.",
                  "input_schema": {"type": "object", "properties": {}}})
    tools.append({"name": "chat", "description": "Conversation or a question to answer by talking.",
                  "input_schema": {"type": "object", "properties": {}}})
    return tools


def _context(recent: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {str(m['content'])[:200]}" for m in recent[-6:])


class Decider(ABC):
    name: str

    @abstractmethod
    async def decide(self, text: str, recent: list[dict], job: str | None = None) -> Route | None:
        """Return a Route, or None when unsure (the next decider gets a go)."""


class LLMDecider(Decider):
    """Claude picks the route (and the action's arguments) with one forced tool call."""
    name = "llm"

    def __init__(self, model: str) -> None:
        self.model = model

    async def decide(self, text: str, recent: list[dict], job: str | None = None) -> Route | None:
        return await asyncio.to_thread(self._sync, text, recent, job)

    def _sync(self, text: str, recent: list[dict], job: str | None = None) -> Route:
        calls = llm.current().tool_calls(
            model=self.model, max_tokens=200, system=ROUTER_SYSTEM, tools=_llm_tools(), tool_choice={"type": "any"},
            messages=[{"role": "user", "content": f"Recent exchange:\n{_context(recent) or '(none)'}\n\n"
                                                  f"Task currently running: {job or 'none'}\n\nRequest: {text}"}],
        )
        for c in calls:
            if c.name == "control":
                return Route("control", how="llm") if job else Route("chat", how="llm")
            if c.name == "agent_task":
                return Route("agent", how="llm")
            if c.name == "chat":
                return Route("chat", how="llm")
            if c.name in actions.all():
                return Route("action", actions.get(c.name), {k: str(v) for k, v in c.args.items()}, how="llm")
        return Route("chat", how="llm")


class JevDecider(Decider):
    """TypeSafe Jev: a System One model that answers typed questions in one fast call, with calibrated
    confidence. It classifies (route + which action); it cannot extract free-text arguments, so those come
    from the regex patterns, else one tiny forced tool call. Below `min_confidence` we return None and the
    LLM decider takes over."""
    name = "jev"

    def __init__(self, min_confidence: float = 0.6, model: str | None = None, timeout_s: float = 4.0,
                 arg_model: str = "claude-haiku-4-5", client=None, allow_extraction: bool = True) -> None:
        self.min_confidence, self.model, self.timeout_s, self.arg_model = min_confidence, model, timeout_s, arg_model
        self._client = client
        self.allow_extraction = allow_extraction      # False: only act when the arguments can be read exactly (no guessing)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("TYPESAFE_API_KEY"))

    def _get_client(self):
        if self._client is None:
            from typesafe_sdk import AsyncTypeSafeClient
            self._client = AsyncTypeSafeClient()
        return self._client

    async def read(self, text: str, recent: list[dict], job: str | None = None) -> JevRead:
        """One call, three answers: route, which action, and how complex the request is."""
        from typesafe_sdk import Choice
        action_names = {n: c.description for n, c in actions.all().items()}
        questions = {
            "route": Choice(instructions=ROUTE_QUESTION,
                            criteria={"action": "one simple computer command", "agent": "browser, page, files or multi-step real work",
                                      "chat": "conversation or a question to answer by talking",
                                      "control": "stop or cancel the running task"}),
            "action": Choice(instructions="If this is a simple computer command, which one is it? Choose none if it is not one, and for news or research questions.",
                             criteria={**action_names, "none": "not a simple command"}),
            "complexity": Choice(instructions="How big is this request? Count the distinct things that must be done.",
                                 criteria={"none": "no task: conversation, or a question to answer by talking",
                                           "single": "one step: one command, one page action or one lookup",
                                           "multi": "a few steps (about three to five), e.g. search a site, open a result, report back",
                                           "extended": "many steps, several sites or sources, comparing or ranking, reading files "
                                                       "plus browsing, or anything consequential like applying, sending or buying"}),
        }
        state = {"request": text, "recent_exchange": _context(recent) or "(none)", "task_running": job or "none"}
        kwargs = {"model": self.model} if self.model else {}
        resp = await asyncio.wait_for(self._get_client().system_one(state=state, questions=questions, **kwargs), self.timeout_s)
        r, a, c = resp.choices["route"], resp.choices["action"], resp.choices.get("complexity")
        return JevRead(r.choice, r.confidence, a.choice, a.confidence, c.choice if c else None, c.confidence if c else 0.0)

    async def decide(self, text: str, recent: list[dict], job: str | None = None) -> Route | None:
        read = await self.read(text, recent, job)
        route = type("R", (), {"choice": read.route, "confidence": read.route_conf})
        if route.confidence < self.min_confidence:
            log.info("Jev unsure (%s %.2f); falling back", route.choice, route.confidence)
            return None
        if route.choice == "control" and not job:
            return Route("chat", how="jev", confidence=route.confidence)
        if route.choice != "action":
            return Route(route.choice, how="jev", confidence=route.confidence)

        act = type("A", (), {"choice": read.action, "confidence": read.action_conf})
        if act.choice == "none" or act.confidence < self.min_confidence or act.choice not in actions.all():
            return None
        cls = actions.get(act.choice)
        args = cls.match(text) if cls.params else {}
        if cls.params and args is None:
            if not self.allow_extraction:
                return None
            args = await asyncio.to_thread(self._extract_args, cls, text)
        return Route("action", cls, args or {}, how="jev", confidence=min(route.confidence, act.confidence))

    def _extract_args(self, cls: type[Action], text: str) -> dict[str, str]:
        """Jev picks the action; a forced tool call fills in its arguments (names, queries, levels)."""
        for c in llm.current().tool_calls(model=self.arg_model, max_tokens=150, tools=[cls.spec()],
                                          tool_choice={"type": "tool", "name": cls.name}, messages=[{"role": "user", "content": text}]):
            return {k: str(v) for k, v in c.args.items()}
        return {}


def build_deciders(config: Config) -> list[Decider]:
    """assistant.router: jev | llm | auto (auto = Jev when TYPESAFE_API_KEY is set, with the LLM as the fallback)."""
    mode = config.get("assistant.router", "auto")
    llm = LLMDecider(config.get("assistant.router_model", "claude-haiku-4-5"))
    jev = JevDecider(config.get("assistant.jev.min_confidence", 0.6), config.get("assistant.jev.model"),
                     config.get("assistant.jev.timeout_s", 4.0), config.get("assistant.router_model", "claude-haiku-4-5"))
    if mode == "llm" or (mode == "auto" and not JevDecider.available()):
        return [llm]
    if mode == "jev" and not JevDecider.available():
        log.warning("assistant.router is 'jev' but TYPESAFE_API_KEY is not set; using the LLM router")
        return [llm]
    return [jev, llm]


STOP = re.compile(r"^(?:ok(?:ay)?[, ]+)?(?:stop|cancel|abort|never ?mind|forget it|hold on|wait|enough)(?: (?:that|it|this|the task|please|now))*\W*$", re.I)
_FILL = r"(?:[, ]+(?:please|thanks|thank you|go ahead|do it|sure|yes|yeah|that'?s fine|for me|now))*\W*$"
YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|approved?|confirm(?:ed)?|proceed|please do|"
                 r"that'?s fine|sounds good|absolutely|definitely|affirmative)" + _FILL, re.I)
NO = re.compile(r"^(?:no|nope|nah|don'?t|do not|deny|denied|cancel|stop|negative|not now|not yet|absolutely not)"
                r"(?:[, ]+(?:please|thanks|do that|do it|that|it|don'?t))*\W*$", re.I)
SCREEN_FOLLOWUP = re.compile(
    r"^(?:scroll|page (?:up|down)|go (?:back|forward)|(?:next|previous) page|click|tap|press|select|reload|refresh|zoom|"
    r"open (?:the )?(?:first|second|third|fourth|fifth|last|next|previous|top)\b|open (?:that|this|it)\b|"
    r"read (?:it|that|this|the page|the article)|what(?:'s| is) on (?:the |my )?(?:screen|page)|"
    r"look at (?:the |my )?(?:screen|page)|take a screenshot|scroll to (?:the )?(?:top|bottom))", re.I)


def parse_yes_no(text: str) -> bool | None:
    t = text.strip()
    if YES.match(t):
        return True
    if NO.match(t):
        return False
    return None


def fast_route(text: str, job: str | None = None) -> Route | None:
    """The instant, no-model paths: stop commands, on-screen follow-ups, and short commands that match an action pattern.
    None means a model must decide."""
    plain = normalize(text)
    if job and STOP.match(plain):
        return Route("control", how="pattern")
    if SCREEN_FOLLOWUP.match(plain):
        return Route("agent", how="pattern")
    if len(plain.split()) <= FAST_PATH_MAX_WORDS and (hit := match_pattern(plain)):
        return Route("action", hit[0], hit[1], how="pattern")
    return None


async def route(text: str, recent: list[dict], config: Config, deciders: list[Decider] | None = None,
                job: str | None = None) -> Route:
    if (fast := fast_route(text, job)) is not None:
        return fast
    for d in deciders or build_deciders(config):
        try:
            if (r := await d.decide(text, recent, job)) is not None:
                return r
        except Exception as e:                       # network, auth, rate limit: never block the user on the router
            log.warning("%s router failed (%s: %s); trying next", d.name, type(e).__name__, e)
    return Route("chat", how="default")
