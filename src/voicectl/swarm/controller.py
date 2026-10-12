from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from voicectl.core.events import EventBus
from voicectl.swarm.board import Board
from voicectl.swarm.link import Link, LinkState, LinkUnavailable
from voicectl.swarm.router import Router

log = logging.getLogger("voicectl.swarm.controller")


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
        if changed and event.get("type") in ("done", "failed"):
            self._resolve(event.get("id", ""))
        elif changed and event.get("type") == "approval":
            self.bus.publish("approval", text=f"{pc}: {event.get('text', 'Approve this action?')}")      # the usual Approve / Deny panel

    def _resolve(self, task_id: str) -> None:
        fut = self._waits.pop(task_id, None)
        if fut and not fut.done():
            fut.set_result(None)

    # ---- one request ----
    async def run(self, text: str, context: str = "") -> str:
        await self.cancel_all()                              # a new request replaces whatever was running
        online = {n for n, link in self.links.items() if link.state is LinkState.ONLINE}
        routed = await self.router.route(text, self.board.picked, online)
        self.board.set_routing(routed.how)
        involved = {a.pc for a in routed.assignments}
        for name, state in list(self.board.pcs.items()):                         # last request's cards must not linger: it looks like they did this one too
            if name not in involved and state.run in ("idle", "done", "failed"):
                self.board.reset(name)
        started: list[tuple[str, str]] = []
        for a in routed.assignments:
            task_id = uuid.uuid4().hex[:12]
            self.board.start(a.pc, task_id, a.task)
            fut = asyncio.get_running_loop().create_future()
            self._waits[task_id] = fut
            try:
                if a.pc not in online:
                    raise LinkUnavailable(f"{a.pc} is {self.links[a.pc].state.value}")
                await self.links[a.pc].send_task(task_id, a.task, context)
                started.append((a.pc, task_id))
            except LinkUnavailable as e:
                self.board.fail(a.pc, str(e))
                self._resolve(task_id)
        self.publish_now()
        try:
            await asyncio.gather(*(self._waits[t] for _, t in started if t in self._waits))
        except asyncio.CancelledError:
            await self.cancel_all()                          # F10 / Esc while working: stop the work everywhere
            raise
        return self._summary(routed.assignments)

    def _summary(self, assignments) -> str:
        parts = []
        for a in assignments:
            s = self.board.pcs[a.pc]
            parts.append(f"{a.pc}: {s.result or s.error or s.run}")
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
