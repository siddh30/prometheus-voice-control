from __future__ import annotations

"""The voice assistant's hands on the rest of your computers: tools that talk to the swarm hub's HTTP API. This is how "one voice" works:
you talk to the Prometheus on the Mac in front of you; when a task belongs elsewhere it sends it through these tools, narrates the
progress and speaks the combined result, while the other computers work silently."""

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("voicectl.swarm.tools")


def api_base(config) -> str:
    """Where the hub's API is: `swarm.api_url`, else the hub address with the next port (ws://host:8780 -> http://host:8781)."""
    if url := config.get("swarm.api_url", ""):
        return url.rstrip("/")
    u = urlparse(config.get("swarm.hub_url", "ws://localhost:8780"))
    port = int(config.get("swarm.api_port", (u.port or 8780) + 1))
    return f"{'https' if u.scheme == 'wss' else 'http'}://{u.hostname or 'localhost'}:{port}"


def _text(s: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if error else {})}


def format_job(job: dict) -> str:
    subs = job.get("subs", [])
    if job.get("result") and (len(subs) != 1 or not subs[0].get("result")):
        head = job["result"]
    else:
        head = ""
    parts = [f"{s['node']} ({s['state']}): {s['result']}" if s.get("result") else f"{s['node']} ({s['state']})" for s in subs]
    body = "\n".join(parts) if len(subs) > 1 else (subs[0]["result"] if subs and subs[0].get("result") else (head or "Nothing came back."))
    return body if len(subs) <= 1 else (head + "\n" if head else "") + body


def build_swarm_tools(base: str, token: str, bus=None, max_wait_s: float = 300.0) -> list:
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def call(method: str, path: str, **kw) -> dict | list:
        timeout = aiohttp.ClientTimeout(total=kw.pop("timeout", 130))
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.request(method, base + path, **kw) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise RuntimeError(data.get("error", f"HTTP {r.status}") if isinstance(data, dict) else f"HTTP {r.status}")
                return data

    async def safely(coro) -> dict[str, Any]:
        try:
            return await coro
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return _text("I can't reach the swarm hub right now. Say so, and offer to do it on this computer instead.", error=True)
        except Exception as e:
            return _text(f"{e}", error=True)

    @tool("swarm_computers", "List the user's OTHER computers in the swarm: who is online and whether they are busy.", {})
    async def computers(args):
        async def go():
            rows = await call("GET", "/api/computers")
            return _text("\n".join(f"{r['name']}: {r['status']}" + (f" ({r['description']})" if r.get("description") else "") for r in rows)
                         or "No computers are in the swarm.")
        return await safely(go())

    @tool("swarm_run", "Send a task to one of the user's computers (computer = its name, e.g. 'Mac mini') or, with computer empty, let the swarm "
                       "choose and split the work. Use it when the user names another computer, asks for several, or the task belongs elsewhere. "
                       "Waits for the result and returns it. The task must be self-contained.", {"task": str, "computer": str})
    async def run(args):
        async def go():
            job = await call("POST", "/api/jobs", json={"text": args["task"], "computer": args.get("computer", "") or ""})
            seen: set[str] = set()
            waited = 0.0
            while waited < max_wait_s:
                job = await call("GET", f"/api/jobs/{job['id']}/wait?timeout=20")
                waited += 20
                if bus is not None:                                    # show the other computers' progress on this Mac's widget too
                    for s in job.get("subs", []):
                        for st in s.get("steps", []):
                            key = f"{s['id']}:{st['text']}"
                            if st["text"] and key not in seen:
                                seen.add(key)
                                bus.publish("step", text=f"{s['node']}: {st['text']}", done=bool(st.get("done")), failed=bool(st.get("failed")))
                asking = [s for s in job.get("subs", []) if s.get("approval")]
                if asking:
                    s = asking[0]
                    return _text(f"{s['node']} is asking for approval: {s['approval']} (sub id {s['id']}). Ask the user, then call swarm_approve.")
                if job["state"] != "running":
                    return _text(format_job(job), error=job["state"] == "failed")
            return _text(f"Still running after {int(max_wait_s)} seconds (job {job['id']}). Tell the user, and check with swarm_status.")
        return await safely(go())

    @tool("swarm_status", "Check a swarm job by its id.", {"job": str})
    async def status(args):
        async def go():
            job = await call("GET", f"/api/jobs/{args['job']}")
            return _text(f"{job['state']}: " + format_job(job))
        return await safely(go())

    @tool("swarm_approve", "Answer an approval a computer is waiting for (sub = the sub id it gave, ok = true to approve, false to deny).", {"sub": str, "ok": bool})
    async def approve(args):
        async def go():
            await call("POST", f"/api/subs/{args['sub']}/approve", json={"ok": bool(args.get("ok"))})
            return _text("Approved." if args.get("ok") else "Denied.")
        return await safely(go())

    @tool("swarm_cancel", "Stop a swarm job by its id.", {"job": str})
    async def cancel(args):
        async def go():
            await call("POST", f"/api/jobs/{args['job']}/cancel")
            return _text("Cancelled.")
        return await safely(go())

    return [computers, run, status, approve, cancel]


def build_swarm_server(base: str, token: str, bus=None, max_wait_s: float = 300.0):
    return create_sdk_mcp_server("swarm", tools=build_swarm_tools(base, token, bus, max_wait_s))
