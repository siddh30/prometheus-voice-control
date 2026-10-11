from __future__ import annotations

"""The Prometheus Swarm hub: knows which of your computers are online, splits a request, hands each part to a node, relays progress to
every dashboard (a phone is just a browser), and brings the results back together.

One port serves everything: the dashboard page (GET /), the nodes (WebSocket /node) and the dashboards (WebSocket /ui). Every
connection needs the shared token. It is meant for your own network; do not expose it to the internet."""

import asyncio
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

from voicectl.swarm.planner import Assignment, NoNodes, plan as make_plan

log = logging.getLogger("voicectl.swarm.hub")
WEB = Path(__file__).parent / "web"
MAX_TASK_S = 20 * 60                       # a task that runs longer than this is cut off


@dataclass
class Node:
    name: str
    ws: ServerConnection
    description: str = ""
    caps: list[str] = field(default_factory=list)
    os: str = ""
    status: str = "idle"                   # idle | busy
    last_seen: float = field(default_factory=time.time)
    sub: str = ""                          # the subtask it is running

    def public(self) -> dict:
        return {"name": self.name, "status": self.status, "online": True, "description": self.description, "caps": self.caps,
                "os": self.os, "sub": self.sub}


@dataclass
class Sub:
    id: str
    job: str
    node: str
    text: str
    why: str = ""
    state: str = "queued"                  # queued | running | done | failed | cancelled
    steps: list[dict] = field(default_factory=list)
    checklist: list[dict] = field(default_factory=list)
    result: str = ""
    approval: str = ""
    started: float = 0.0
    ended: float = 0.0

    def public(self) -> dict:
        return {"id": self.id, "job": self.job, "node": self.node, "text": self.text, "why": self.why, "state": self.state,
                "steps": self.steps[-8:], "checklist": self.checklist, "result": self.result, "approval": self.approval,
                "started": self.started, "ended": self.ended}


@dataclass
class Job:
    id: str
    request: str
    subs: list[Sub] = field(default_factory=list)
    state: str = "running"                 # running | done | failed | cancelled
    result: str = ""
    started: float = field(default_factory=time.time)
    ended: float = 0.0

    def public(self) -> dict:
        return {"id": self.id, "request": self.request, "state": self.state, "result": self.result, "started": self.started,
                "ended": self.ended, "subs": [s.public() for s in self.subs]}


Summarizer = Callable[[Job], Awaitable[str]]


async def join_results(job: Job) -> str:
    """The fallback way of combining results: one part is its own result; several are listed by machine."""
    done = [s for s in job.subs if s.result]
    if len(done) == 1:
        return done[0].result
    return "\n\n".join(f"{s.node}: {s.result}" for s in done) or "Nothing came back."


class Hub:
    def __init__(self, host: str = "0.0.0.0", port: int = 8780, token: str = "", llm=None, summarizer: Summarizer = join_results,
                 default_node: str = "", offline_grace_s: float = 0.0) -> None:
        self.host, self.port, self.token = host, port, token
        self.llm, self.summarizer, self.default_node = llm, summarizer, default_node
        self.nodes: dict[str, Node] = {}
        self.known: dict[str, dict] = {}                 # every node ever seen, so offline machines still show on the dashboard
        self.jobs: dict[str, Job] = {}
        self.subs: dict[str, Sub] = {}
        self.uis: set[ServerConnection] = set()
        self._queues: dict[str, list[str]] = {}
        self._server = None

    # ---------- plumbing ----------
    def _authorized(self, request: Request) -> bool:
        if not self.token:
            return True
        q = parse_qs(urlparse(request.path).query).get("token", [""])[0]
        return hmac.compare_digest(q, self.token)

    def _process_request(self, connection: ServerConnection, request: Request):
        path = urlparse(request.path).path
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if path not in ("/node", "/ui") or not self._authorized(request):
                return connection.respond(401 if path in ("/node", "/ui") else 404, "not allowed\n")
            return None
        if path in ("/", "/index.html"):
            body = (WEB / "index.html").read_bytes()
            return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                                                 ("Cache-Control", "no-store")]), body)
        if path == "/health":
            return connection.respond(200, "ok\n")
        return connection.respond(404, "not found\n")

    async def _broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        for ws in list(self.uis):
            try:
                await ws.send(text)
            except Exception:
                self.uis.discard(ws)

    def snapshot(self) -> dict:
        nodes = [{**info, "online": False, "status": "offline"} for name, info in self.known.items() if name not in self.nodes]
        nodes += [n.public() for n in self.nodes.values()]
        return {"type": "snapshot", "nodes": sorted(nodes, key=lambda n: n["name"]),
                "jobs": [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.started)[-12:]]}

    async def _send(self, node: Node, msg: dict) -> None:
        try:
            await node.ws.send(json.dumps(msg))
        except Exception as e:
            log.info("couldn't reach %s: %s", node.name, e)

    # ---------- nodes ----------
    async def _node_handler(self, ws: ServerConnection) -> None:
        node: Node | None = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "hello":
                    name = str(msg.get("name", "")).strip()[:40]
                    if not name:
                        await ws.close(1008, "a node needs a name")
                        return
                    old = self.nodes.get(name)
                    if old is not None:                      # the same machine reconnecting: the new connection wins
                        try:
                            await old.ws.close(1000, "replaced")
                        except Exception:
                            pass
                    node = Node(name, ws, str(msg.get("description", ""))[:200], list(msg.get("caps", []))[:12], str(msg.get("os", ""))[:30])
                    self.nodes[name] = node
                    self.known[name] = {"name": name, "description": node.description, "caps": node.caps, "os": node.os}
                    log.info("node %s joined (%s)", name, node.description or "no description")
                    await self._broadcast({"type": "node", "node": node.public()})
                    await self._dispatch(name)
                elif node is None:
                    continue
                else:
                    node.last_seen = time.time()
                    await self._on_node_message(node, msg)
        except Exception as e:
            log.debug("node connection ended: %s", e)
        finally:
            if node is not None and self.nodes.get(node.name) is node:
                await self._node_left(node)

    async def _node_left(self, node: Node) -> None:
        self.nodes.pop(node.name, None)
        log.info("node %s left", node.name)
        if node.sub and (sub := self.subs.get(node.sub)) and sub.state == "running":
            await self._finish_sub(sub, "failed", f"{node.name} went offline while working on this.")
        await self._broadcast({"type": "node", "node": {**self.known.get(node.name, {"name": node.name}), "online": False, "status": "offline"}})

    async def _on_node_message(self, node: Node, msg: dict) -> None:
        sub = self.subs.get(str(msg.get("sub", "")))
        kind = msg.get("type")
        if kind == "heartbeat" or sub is None:
            return
        if kind == "step":
            sub.steps.append({"text": str(msg.get("text", ""))[:160], "done": bool(msg.get("done")), "failed": bool(msg.get("failed")), "t": time.time()})
            await self._broadcast({"type": "sub", "sub": sub.public()})
        elif kind == "checklist":
            sub.checklist = [{"text": str(i.get("text", ""))[:90], "status": str(i.get("status", "pending"))} for i in list(msg.get("items", []))[:10]]
            await self._broadcast({"type": "sub", "sub": sub.public()})
        elif kind == "approval":
            sub.approval = str(msg.get("text", ""))[:300]
            await self._broadcast({"type": "sub", "sub": sub.public()})
        elif kind == "done":
            await self._finish_sub(sub, "done" if msg.get("ok", True) else "failed", str(msg.get("result", ""))[:4000])

    # ---------- jobs ----------
    async def submit(self, request: str) -> Job | None:
        request = request.strip()
        if not request:
            return None
        job = Job(uuid.uuid4().hex[:8], request)
        self.jobs[job.id] = job
        try:
            nodes = [n.public() for n in self.nodes.values()]
            assignments = await make_plan(request, nodes, self.llm, self.default_node)
        except NoNodes as e:
            job.state, job.result, job.ended = "failed", f"I can't do that right now: {e}.", time.time()
            await self._broadcast({"type": "job", "job": job.public()})
            return job
        for a in assignments:
            sub = Sub(uuid.uuid4().hex[:8], job.id, a.node, a.task, a.why)
            job.subs.append(sub)
            self.subs[sub.id] = sub
            self._queues.setdefault(a.node, []).append(sub.id)
        await self._broadcast({"type": "job", "job": job.public()})
        for a in assignments:
            await self._dispatch(a.node)
        return job

    async def _dispatch(self, name: str) -> None:
        node = self.nodes.get(name)
        if node is None or node.status == "busy":
            return
        queue = self._queues.get(name, [])
        while queue:
            sub = self.subs.get(queue.pop(0))
            if sub is None or sub.state != "queued":
                continue
            sub.state, sub.started = "running", time.time()
            node.status, node.sub = "busy", sub.id
            await self._send(node, {"type": "task", "sub": sub.id, "text": sub.text})
            asyncio.get_running_loop().call_later(MAX_TASK_S, lambda s=sub: asyncio.ensure_future(self._timeout(s)))
            await self._broadcast({"type": "node", "node": node.public()})
            await self._broadcast({"type": "sub", "sub": sub.public()})
            return

    async def _timeout(self, sub: Sub) -> None:
        if sub.state == "running":
            node = self.nodes.get(sub.node)
            if node:
                await self._send(node, {"type": "cancel", "sub": sub.id})
            await self._finish_sub(sub, "failed", "That took too long, so I stopped it.")

    async def _finish_sub(self, sub: Sub, state: str, result: str) -> None:
        if sub.state in ("done", "failed", "cancelled"):
            return
        sub.state, sub.result, sub.ended, sub.approval = state, result, time.time(), ""
        node = self.nodes.get(sub.node)
        if node is not None and node.sub == sub.id:
            node.status, node.sub = "idle", ""
            await self._broadcast({"type": "node", "node": node.public()})
        await self._broadcast({"type": "sub", "sub": sub.public()})
        job = self.jobs.get(sub.job)
        if job and all(s.state in ("done", "failed", "cancelled") for s in job.subs) and job.state == "running":
            job.state = "done" if any(s.state == "done" for s in job.subs) else ("cancelled" if all(s.state == "cancelled" for s in job.subs) else "failed")
            try:
                job.result = await asyncio.wait_for(self.summarizer(job), 20)
            except Exception:
                job.result = await join_results(job)
            job.ended = time.time()
            await self._broadcast({"type": "job", "job": job.public()})
        await self._dispatch(sub.node)

    async def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        for sub in job.subs:
            if sub.state == "queued":
                await self._finish_sub(sub, "cancelled", "Cancelled.")
            elif sub.state == "running":
                node = self.nodes.get(sub.node)
                if node:
                    await self._send(node, {"type": "cancel", "sub": sub.id})
                await self._finish_sub(sub, "cancelled", "Cancelled.")

    async def approve(self, sub_id: str, ok: bool) -> None:
        sub = self.subs.get(sub_id)
        node = self.nodes.get(sub.node) if sub else None
        if sub and node:
            sub.approval = ""
            await self._send(node, {"type": "approve", "sub": sub.id, "ok": ok})
            await self._broadcast({"type": "sub", "sub": sub.public()})

    # ---------- dashboards ----------
    async def _ui_handler(self, ws: ServerConnection) -> None:
        self.uis.add(ws)
        try:
            await ws.send(json.dumps(self.snapshot()))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "submit":
                    asyncio.get_running_loop().create_task(self.submit(str(msg.get("text", ""))))
                elif kind == "cancel":
                    await self.cancel(str(msg.get("job", "")))
                elif kind == "approve":
                    await self.approve(str(msg.get("sub", "")), bool(msg.get("ok")))
        finally:
            self.uis.discard(ws)

    async def _handler(self, ws: ServerConnection) -> None:
        path = urlparse(ws.request.path).path
        await (self._node_handler(ws) if path == "/node" else self._ui_handler(ws))

    async def start(self) -> None:
        self._server = await serve(self._handler, self.host, self.port, process_request=self._process_request,
                                   max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=30)
        log.info("Prometheus Swarm hub on http://%s:%d", self.host, self.bound_port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def run(self) -> None:
        await self.start()
        await asyncio.Future()

    @property
    def bound_port(self) -> int:
        return self._server.sockets[0].getsockname()[1] if self._server else self.port
