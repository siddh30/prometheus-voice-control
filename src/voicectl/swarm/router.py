from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from voicectl import llm

log = logging.getLogger("voicectl.swarm.router")


LOCAL_PHRASES = ("this mac", "this computer", "this machine", "this pc", "this laptop", "this one", "right here", "here", "locally")


@dataclass(frozen=True)
class PC:
    name: str
    aliases: tuple[str, ...] = ()
    local: bool = False                     # the computer you are speaking on

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n.lower() for n in (self.name, *self.aliases) if n))

    @property
    def here(self) -> tuple[str, ...]:
        """How you point at the computer you are speaking on."""
        return LOCAL_PHRASES if self.local else ()


@dataclass(frozen=True)
class Assignment:
    pc: str
    task: str


@dataclass
class Routed:
    assignments: list[Assignment] = field(default_factory=list)
    how: str = "default"                    # picked | named | all | other | llm | split | default (shown on the widget, useful in logs)


DEVICE = r"(?:computers?|pcs?|macs?|machines?|laptops?|devices?)"
# Only a request counts: "on both", "both computers", "all my computers", "everywhere". "I think you can control both" does not.
ALL = re.compile(rf"\b(?:(?:on|in|across|for)\s+both(?:\s+of\s+(?:them|my\s+{DEVICE}))?(?:\s+{DEVICE})?|both\s+{DEVICE}|both\s+of\s+(?:them|my\s+{DEVICE})"
                 rf"|all(?:\s+of)?(?:\s+(?:my|the))?\s+{DEVICE}|all\s+of\s+them|every\s+{DEVICE}|everywhere)\b", re.I)
# "on different computers", "one on each", "split it": the request has to be divided, which only a model can do
SPLIT = re.compile(rf"\b(?:different|separate|each|several|multiple)\s+{DEVICE}\b|\bone\s+(?:on|per)\s+each\b|\bone\s+each\b|\bsplit\s+(?:it|them|this|that)\b"
                   rf"|\b(?:different|separate)\s+(?:results?|tests?|things?|tasks?|jobs?|searches|ones?)\b|\bthe\s+other\s+(?:thing|task|job|test)\b", re.I)
OTHER = re.compile(r"\b(?:on\s+)?(?:the\s+|my\s+)?(?:other|another)\s+(?:one|computer|pc|mac|machine|laptop)\b|\belsewhere\b", re.I)
LEAD = r"(?:on|to|in|from|using|with|at|of|for|via|through|about|ask|tell|check|test(?:\s+on)?|connect(?:\s+to)?)\s+(?:my\s+|the\s+)?"
TASK_SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+)?|\band\s+(?:then\s+)?|\bthen\s+)\s*(?=\S)", re.I)

ASSIGN_TOOL = {
    "name": "assign",
    "description": "Split the user's request into tasks and say which computer does each. Use only the computers listed.",
    "input_schema": {"type": "object", "properties": {"assignments": {"type": "array", "items": {
        "type": "object", "properties": {"pc": {"type": "string"}, "task": {"type": "string"}}, "required": ["pc", "task"]}}},
        "required": ["assignments"]},
}


class Router:
    """Decides which computer gets what. Rules first, because they are instant and predictable:
      * a computer picked on the widget gets the whole request;
      * "on the MacBook", "on both", "on the other computer" are understood directly;
      * a request with no hint goes to this computer.
    Only a request that must be split across three or more computers needs the (small, fast) model, and it falls back to this computer."""

    def __init__(self, pcs: list[PC], model: str = "claude-haiku-4-5", timeout_s: float = 4.0) -> None:
        self.pcs, self.model, self.timeout_s = pcs, model, timeout_s

    @property
    def local(self) -> PC:
        return next((p for p in self.pcs if p.local), self.pcs[0])

    def _find(self, name: str) -> PC | None:
        name = name.lower().strip()
        return next((p for p in self.pcs if name in p.names), None)

    def _alias_regex(self, pc: PC) -> re.Pattern:
        alts = "|".join(re.escape(n) for n in sorted(pc.names, key=len, reverse=True))
        pattern = rf"\b{LEAD}(?:{alts})\b|\b(?:the|my)\s+(?:{alts})\b|^(?:hey\s+|ok(?:ay)?\s+)?(?:the\s+|my\s+)?(?:{alts})\s*[,:]\s*"      # "on the MacBook", "of the MacBook", "MacBook, open it"
        if pc.here:                                                              # "this Mac" / "here" need no lead word
            here = "|".join(re.escape(n) for n in sorted(pc.here, key=len, reverse=True))
            pattern += rf"|\b(?:{LEAD})?(?:{here})\b"
        return re.compile(pattern, re.I)

    async def route(self, text: str, picked: str | None = None, online: set[str] | None = None, hint: str = "") -> Routed:
        online = {p.name for p in self.pcs} if online is None else online
        usable = [p for p in self.pcs if p.name in online]
        text = text.strip()

        if picked and (pc := self._find(picked)):
            return Routed([Assignment(pc.name, text)], "picked")

        if SPLIT.search(text) and len(usable) > 1:                                  # "on different computers", "one each": the request must be divided, even if it also says "both"
            asked = await self._ask_model(text, usable, spread=True, hint=hint)
            if asked.how == "llm":
                return Routed(asked.assignments, "split")

        if ALL.search(text):
            task = re.sub(r"\s{2,}", " ", ALL.sub(" ", text)).strip(" ,.") or text
            return Routed([Assignment(p.name, task) for p in usable or self.pcs], "all")

        named, guessed = self._named_clauses(text)
        if named and guessed and len(usable) > 1:                                    # some parts name a computer and some do not: don't guess who gets those
            asked = await self._ask_model(text, usable, hint=hint)
            if asked.how == "llm":
                return asked
        if named:
            return Routed(named, "named")

        if OTHER.search(text):
            others = [p for p in usable if not p.local]
            task = re.sub(r"\s{2,}", " ", OTHER.sub(" ", text)).strip(" ,.") or text
            if len(task.split()) < 2 or re.match(r"(?:what|how)\s+about\b", task, re.I):    # "what about the other Mac?" left as "what about ?": keep the words, the context carries the rest
                task = text
            if len(others) == 1:
                return Routed([Assignment(others[0].name, task)], "other")
            if len(others) > 1:
                return await self._ask_model(text, usable, hint=hint)

        if len(usable) > 1 and self._multi_part(text):                               # several jobs, no computer named: spread them out
            asked = await self._ask_model(text, usable, spread=True, hint=hint)
            if asked.how == "llm":
                return Routed(asked.assignments, "split")

        return Routed([Assignment(self.local.name, text)], "default")

    @staticmethod
    def _multi_part(text: str) -> bool:
        """"open gmail and play music": two jobs. A single job never needs the model."""
        return sum(1 for c in TASK_SPLIT.split(text) if len(c.split()) >= 2) >= 2

    @staticmethod
    def _unique(items: list[Assignment]) -> list[Assignment]:
        """Never give two computers the same job: a repeat of an earlier part (on another computer) is dropped.
        Only an explicit "both" / "on the MacBook and the mini" sends one job to two computers."""
        out: list[Assignment] = []
        seen: list[set[str]] = []
        for a in items:
            words = set(re.findall(r"[a-z0-9']+", a.task.lower()))
            if any(len(words & w) / max(1, len(words | w)) >= 0.7 for w in seen):
                continue
            seen.append(words)
            out.append(a)
        return out

    def _named_clauses(self, text: str) -> tuple[list[Assignment], bool]:
        """"open Gmail on the MacBook and play music on the Mac mini" -> one task each. A clause naming two computers goes to both."""
        clauses = [c for c in TASK_SPLIT.split(text) if c.strip()] or [text]
        out: list[Assignment] = []
        guessed = False                                              # an unnamed part was attached to a named one: only a guess
        carry = ""
        last_task = ""
        lead: list[PC] = []
        for clause in clauses:
            bare = next((p for p in self.pcs if re.fullmatch(rf"(?:the\s+|my\s+)?(?:{'|'.join(re.escape(n) for n in p.names)})", clause.strip(" ,."), re.I)), None)
            if bare and out and not carry:                                  # "...on the MacBook and the mac mini": the same task, also there
                out.append(Assignment(bare.name, last_task))
                continue
            if bare and not out and not carry:                              # "MacBook, open my profile": the name leads the next clause
                lead.append(bare)
                continue
            hits = [p for p in self.pcs if self._alias_regex(p).search(clause)]
            if lead and not hits:
                hits, lead = lead, []
            if not hits:
                carry = f"{carry} {clause}".strip()                    # no computer here: it belongs with the next clause that has one
                continue
            task = clause
            for p in hits:
                task = self._alias_regex(p).sub(" ", task)
            task = re.sub(r"\s{2,}", " ", task).strip(" ,.")
            guessed = guessed or bool(carry)
            last_task = task if not carry else f"{carry} {task}".strip()
            for p in hits:
                out.append(Assignment(p.name, last_task))
            carry = ""
        if carry and out:                                              # trailing words with no computer: they belong to the last task
            last = out[-1]
            out[-1] = Assignment(last.pc, f"{last.task} {carry}".strip())
            guessed = True
        return out, guessed

    async def _ask_model(self, text: str, usable: list[PC], spread: bool = False, hint: str = "") -> Routed:
        names = [p.name for p in usable]
        try:
            calls = await asyncio.wait_for(asyncio.to_thread(lambda: llm.current().tool_calls(
                model=self.model, max_tokens=300, tools=[ASSIGN_TOOL], tool_choice={"type": "tool", "name": "assign"},
                system=f"The user has these computers: {', '.join(names)} (the one they are speaking on is {self.local.name}). "
                       "Assign every part of the request to exactly one computer. Never give the same task to two computers. "
                       "Parts that depend on each other (the second needs the first one's page, app or result) go to the same computer. "
                       + (f"What the user said about similar earlier runs (follow it):\n{hint}\n" if hint else "")
                       + ("Spread parts that are independent across the computers; keep a single job, or jobs that belong together, on one. " if spread else ""),
                messages=[{"role": "user", "content": text}])), self.timeout_s)
        except Exception as e:
            log.info("router model unavailable (%s); using this computer", type(e).__name__)
            return Routed([Assignment(self.local.name, text)], "default")
        out = []
        for call in calls:
            for item in call.args.get("assignments", []):
                pc = self._find(str(item.get("pc", "")))
                if pc and pc.name in names and str(item.get("task", "")).strip():
                    out.append(Assignment(pc.name, str(item["task"]).strip()))
        out = self._unique(out)
        return Routed(out, "llm") if out else Routed([Assignment(self.local.name, text)], "default")
