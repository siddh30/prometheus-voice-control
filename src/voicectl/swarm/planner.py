from __future__ import annotations

"""Who does what. Cheap, deterministic rules first (you named a machine; only one is online); a small model only for the rest;
and a plain fallback if the model is unavailable, so a request is never stuck on planning."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger("voicectl.swarm.planner")


@dataclass(frozen=True)
class Assignment:
    node: str
    task: str
    why: str = ""


class NoNodes(RuntimeError):
    pass


ALIASES = {"macbook": ("macbook", "laptop", "mbp", "mac book"), "mini": ("mac mini", "mini"), "studio": ("mac studio", "studio"),
           "imac": ("imac",), "air": ("macbook air", "air")}
EVERYWHERE = re.compile(r"\b(?:on|using)\s+(?:both|all|each|every)\b|\bboth (?:of )?(?:the |my )?(?:machines|computers|macs)\b|\ball (?:of )?my (?:machines|computers|macs)\b", re.I)


def _mentions(request: str, node: dict) -> bool:
    text = request.lower()
    name = node["name"].lower()
    if re.search(rf"\b{re.escape(name)}\b", text):
        return True
    words = set(re.findall(r"[a-z]+", name))
    for key, forms in ALIASES.items():
        if key in words or any(f in name for f in forms):
            if any(re.search(rf"\b{re.escape(f)}\b", text) for f in forms):
                return True
    return False


def _strip_target(request: str, node: dict) -> str:
    """"on the mac mini, download the report" -> "download the report" (the machine doing it knows it is the one)."""
    t = re.sub(r"^(?:please\s+)?(?:(?:on|using|with)\s+(?:the\s+|my\s+)?[\w ]{2,20}?[,:]\s*)", "", request.strip(), flags=re.I)
    return t or request


LLM = Callable[[str], Awaitable[str]]


async def plan(request: str, nodes: list[dict], llm: LLM | None = None, default: str = "") -> list[Assignment]:
    """`nodes`: [{name, status, description, caps}] for the machines that are online. Returns who does what."""
    online = [n for n in nodes if n.get("online", True)]
    if not online:
        raise NoNodes("none of your computers is online")
    if len(online) == 1:
        return [Assignment(online[0]["name"], request, "the only computer online")]
    named = [n for n in online if _mentions(request, n)]
    if EVERYWHERE.search(request):
        return [Assignment(n["name"], request, "you asked for all of them") for n in online]
    if named:
        return [Assignment(n["name"], _strip_target(request, n) if len(named) == 1 else request, "you named it") for n in named]
    if llm is not None and len(request.split()) >= 6:
        try:
            plan_ = await asyncio.wait_for(_llm_plan(request, online, llm), 12)
            if plan_:
                return plan_
        except Exception as e:
            log.info("planner model unavailable (%s: %s); using a simple rule", type(e).__name__, str(e)[:80])
    return [Assignment(_fallback(online, default), request, "least busy")]


def _fallback(online: list[dict], default: str) -> str:
    idle = [n for n in online if n.get("status") == "idle"] or online
    for n in idle:
        if default and n["name"] == default:
            return n["name"]
    return idle[0]["name"]


PROMPT = """You assign a user's request to their own computers. Computers (all belong to the user):
{nodes}

Request: {request}

Reply with ONLY JSON: {{"assignments": [{{"node": "<exact name>", "task": "<what that computer should do, self-contained>", "why": "<few words>"}}]}}
Rules: use ONE computer unless the request clearly has independent parts that can run in parallel. Prefer the computer that has what the task needs
(its files, its logged-in accounts, being idle). Each task must make sense on its own, without the other tasks."""


async def _llm_plan(request: str, online: list[dict], llm: LLM) -> list[Assignment]:
    listing = "\n".join(f"- {n['name']} ({n.get('status', 'idle')}): {n.get('description') or 'a Mac'}" for n in online)
    raw = await llm(PROMPT.format(nodes=listing, request=request))
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    valid = {n["name"] for n in online}
    out = [Assignment(a["node"], str(a.get("task") or request), str(a.get("why", ""))) for a in data.get("assignments", [])
           if isinstance(a, dict) and a.get("node") in valid]
    return out[:4]                                           # a request can fan out to a handful of machines, never a flood
