from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from voicectl.core.events import EventBus
from voicectl.swarm.board import Board
from voicectl.swarm.link import Link, LinkState, LinkUnavailable
from voicectl.swarm.router import Router

log = logging.getLogger("voicectl.swarm.controller")

# A worker that needs another computer says so in plain words on its last line: "HANDOFF MacBook: open the file I saved and ...".
# No tool and no protocol: it is text in the answer, which the controller reads, removes, and sends on.
HANDOFF = re.compile(r"^[ \t>*-]*HANDOFF\s+([^:\n]+?)\s*:\s*(.+?)\s*$", re.I | re.M)
MAX_HOPS = 3


class SwarmController:
    """The PC you speak on. One request in; the router says who does what; each worker's progress flows into the board; the
    board is published to the widget a few times a second. Nothing here waits on the widget, and nothing the widget does can
    wait on a worker: a silent computer just shows as "not responding"."""

    def __init__(self, links: dict[str, Link], router: Router, bus: EventBus, *, publish_every_s: float = 0.15) -> None:
        self.links, self.router, self.bus, self.publish_every_s = links, router, bus, publish_every_s
        self.board = Board([(p.name, p.local) for p in router.pcs])
        self._waits: dict[str, asyncio.Future] = {}          # task id -> resolved when that task reaches done / failed
        self._flusher: asyncio.Task | None = None
        self._published = -1
        self._hops: dict[str, int] = {}                      # task id -> how many handovers deep it is
        self._seen: set[tuple[str, str]] = set()             # (computer, job) already given in this request: a loop is cut here
        self._involved: list[str] = []                       # every computer that worked on this request, in order

    # ---- lifecycle ----
    async def start(self) -> None:
        for link in self.links.values():
            link.on_event, link.on_state = self._on_event, self._on_state
            self.board.link_state(link.name, link.state, link.detail)
        await asyncio.gather(*(link.start() for link in self.links.values()))
        self._flusher = asyncio.get_running_loop().create_task(self._flush())

    async def stop(self) -> None:
        if self._flusher:
            self._flusher.cancel()
        await asyncio.gather(*(link.stop() for link in self.links.values()), return_exceptions=True)

    # ---- what the widget sees ----
    async def _flush(self) -> None:
        while True:
            if self.board.revision != self._published:
                self._published = self.board.revision
                self.bus.publish("swarm", **self.board.snapshot())
            await asyncio.sleep(self.publish_every_s)

    def publish_now(self) -> None:
        self._published = self.board.revision
        self.bus.publish("swarm", **self.board.snapshot())

    # ---- worker -> controller ----
    def _on_state(self, pc: str, state: LinkState) -> None:
        link = self.links[pc]
        self.board.link_state(pc, state, link.detail)
        if state is LinkState.OFFLINE:                       # whatever it was doing is lost: do not wait for it forever
            self._resolve(self.board.pcs[pc].task_id)

    def _on_event(self, pc: str, event: dict[str, Any]) -> None:
        changed = self.board.apply(pc, event)
        if changed and event.get("type") == "done":
            self._hand_over(pc, event.get("id", ""))             # before the finished task is resolved, so the request knows more work is coming
        if changed and event.get("type") in ("done", "failed"):
            self._resolve(event.get("id", ""))
        elif changed and event.get("type") == "approval":
            self.bus.publish("approval", text=f"{pc}: {event.get('text', 'Approve this action?')}")      # the usual Approve / Deny panel

    def _swarm_note(self, me: str, online: set[str]) -> str:
        """Tells a worker it is one of several computers, and how to pass work on: plain words, no tool."""
        others = [p for p in self.router.pcs if p.name != me and p.name in online]
        if not others:
            return ""
        names = ", ".join(f"{p.name} ({', '.join(p.aliases)})" if p.aliases else p.name for p in others)
        return (f"<Swarm>\nYou are the agent on the computer called {me}. The user's other computers: {names}. Do your own part of the job here. "
                "If a part of it has to happen on another computer, or needs something from it, first do everything you can here, then end your "
                "reply with one line per handover, exactly: HANDOFF <computer name>: <what it must do, with everything it needs from your work>. "
                "Only hand over when the job really needs that computer. Never hand back work you were just handed.\n</Swarm>")

    def _hand_over(self, pc: str, task_id: str) -> None:
        """A finished worker asked another computer to carry on. Start that now, in the same request, with what this one found."""
        s = self.board.pcs[pc]
        found = HANDOFF.findall(s.result)
        if not found:
            return
        s.result = HANDOFF.sub("", s.result).strip()
        hops = self._hops.get(task_id, 0) + 1
        for target, job in found:
            other = self.router._find(target.strip().strip("*`\"'"))
            key = (other.name if other else target.strip().lower(), re.sub(r"\W+", " ", job.lower()).strip())
            if other is None or other.name == pc or hops > MAX_HOPS or key in self._seen:
                log.info("handover from %s to %r ignored (unknown, itself, too deep, or a repeat)", pc, target)
                continue
            self._seen.add(key)
            self._start_task(other.name, job.strip(), f"<HandedOver from=\"{pc}\">\n{s.result or s.task}\n</HandedOver>", hops, origin=pc)
            s.passed_to = other.name
        self.board._touch()

    def _start_task(self, pc: str, text: str, context: str, hops: int, origin: str = "") -> None:
        task_id = uuid.uuid4().hex[:12]
        self.board.start(pc, task_id, text, origin)
        self._hops[task_id] = hops
        if pc not in self._involved:
            self._involved.append(pc)
        fut = asyncio.get_running_loop().create_future()
        self._waits[task_id] = fut
        async def send() -> None:
            try:
                if self.links[pc].state is not LinkState.ONLINE:
                    raise LinkUnavailable(f"{pc} is {self.links[pc].state.value}")
                await self.links[pc].send_task(task_id, text, context)
            except LinkUnavailable as e:
                self.board.fail(pc, str(e))
                self._resolve(task_id)
        asyncio.get_running_loop().create_task(send())

    def _resolve(self, task_id: str) -> None:
        fut = self._waits.pop(task_id, None)
        if fut and not fut.done():
            fut.set_result(None)

    # ---- one request ----
    async def run(self, text: str, context: str = "") -> str:
        await self.cancel_all()                              # a new request replaces whatever was running
        online = {n for n, link in self.links.items() if link.state is LinkState.ONLINE}
        hint = context.split("<PastRuns>")[1].split("</PastRuns>")[0].strip() if "<PastRuns>" in context else ""     # the user's thumbs on similar runs
        routed = await self.router.route(text, self.board.picked, online, hint=hint)
        self.board.set_routing(routed.how)
        involved = {a.pc for a in routed.assignments}
        for name, state in list(self.board.pcs.items()):                         # last request's cards must not linger: it looks like they did this one too
            if name not in involved and state.run in ("idle", "done", "failed"):
                self.board.reset(name)
        self._hops.clear(); self._seen.clear(); self._involved = []
        for a in routed.assignments:
            self._seen.add((a.pc, re.sub(r"\W+", " ", a.task.lower()).strip()))
            self._start_task(a.pc, a.task, f"{self._swarm_note(a.pc, online)}\n{context}".strip(), 0)
        self.publish_now()
        try:
            while self._waits:                               # handovers add work while we wait: keep going until nothing is left
                await asyncio.gather(*list(self._waits.values()))
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self.cancel_all()                          # F10 / Esc while working: stop the work everywhere
            raise
        return self._summary()

    def _summary(self) -> str:
        parts = []
        for name in self._involved:
            s = self.board.pcs[name]
            parts.append(f"{name}: {s.result or s.error or s.run}")
        return "; ".join(parts)

    async def cancel_all(self) -> None:
        running = self.board.working()
        for s in running:
            self._resolve(s.task_id)
        if running:
            await asyncio.gather(*(self._cancel_one(s.name, s.task_id) for s in running), return_exceptions=True)
            for s in running:
                self.board.reset(s.name)
            self.publish_now()

    async def _cancel_one(self, pc: str, task_id: str) -> None:
        try:
            await asyncio.wait_for(self.links[pc].cancel(task_id), 1.0)       # never wait on a computer that is not answering
        except Exception:
            log.debug("could not cancel %s on %s", task_id, pc)

    # ---- the widget's controls ----
    def pick(self, pc: str) -> None:
        self.board.pick(pc)

    @property
    def pending_approval(self) -> str:
        s = self.board.pending_approval()
        return f"{s.name}: {s.approval}" if s else ""

    async def approve(self, approved: bool) -> None:
        s = self.board.pending_approval()
        if not s:
            return
        try:
            await self.links[s.name].approve(s.task_id, approved)
            self.board.apply(s.name, {"type": "resumed", "id": s.task_id})
        except LinkUnavailable as e:
            self.board.fail(s.name, str(e))
            self._resolve(s.task_id)
