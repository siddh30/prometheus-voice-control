from __future__ import annotations

"""The swarm's HTTP API: one small, token-protected surface that everything else uses (the voice assistant's tools, the Swarm widget's
siblings, a phone, an iPhone Shortcut). It is a thin skin over the hub; it holds no state of its own.

  GET  /api/health
  GET  /api/computers                         who is online and what they are doing
  POST /api/jobs        {text, computer?}     start a task (the planner picks the computer unless one is named)
  GET  /api/jobs/{id}                         the job as it stands
  GET  /api/jobs/{id}/wait?timeout=30[&after=N]  wait for it to finish, to need approval, or (with after) for more than N of its
                                              parts to finish; then return it
  POST /api/jobs/{id}/cancel
  POST /api/subs/{id}/approve  {ok}
  GET  /api/events                            live stream (server-sent events)

Send the shared token as `Authorization: Bearer <token>` or `?token=`."""

import asyncio
import hmac
import json

from aiohttp import web


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    given = request.query.get("token", "")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    return hmac.compare_digest(given, token)


def build_app(hub) -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler):
        if request.path != "/api/health" and not _authorized(request, hub.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:                              # an API error is a JSON error, never a crashed hub
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def health(_):
        return web.json_response({"ok": True, "computers": len(hub.nodes)})

    async def computers(_):
        return web.json_response(hub.snapshot()["nodes"])

    async def create_job(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "send JSON: {\"text\": \"...\"}"}, status=400)
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        job = await hub.submit(text, str(body.get("computer", "")))
        return web.json_response(job.public(), status=201)

    async def get_job(request: web.Request):
        job = hub.jobs.get(request.match_info["id"])
        return web.json_response(job.public()) if job else web.json_response({"error": "no such job"}, status=404)

    async def wait_job(request: web.Request):
        timeout = max(0.0, min(float(request.query.get("timeout", 30)), 120.0))
        after = int(request.query["after"]) if "after" in request.query else None
        job = await hub.wait_job(request.match_info["id"], timeout, after)
        return web.json_response(job.public()) if job else web.json_response({"error": "no such job"}, status=404)

    async def cancel_job(request: web.Request):
        await hub.cancel(request.match_info["id"])
        job = hub.jobs.get(request.match_info["id"])
        return web.json_response(job.public()) if job else web.json_response({"error": "no such job"}, status=404)

    async def approve(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        await hub.approve(request.match_info["id"], bool(body.get("ok")))
        return web.json_response({"ok": True})

    async def events(request: web.Request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        hub.listeners.add(q)
        try:
            await resp.write(f"data: {json.dumps(hub.snapshot())}\n\n".encode())
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"data: {json.dumps(msg)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            hub.listeners.discard(q)
        return resp

    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/api/health", health), web.get("/api/computers", computers), web.post("/api/jobs", create_job),
                    web.get("/api/jobs/{id}", get_job), web.get("/api/jobs/{id}/wait", wait_job), web.post("/api/jobs/{id}/cancel", cancel_job),
                    web.post("/api/subs/{id}/approve", approve), web.get("/api/events", events)])
    return app


class ApiServer:
    def __init__(self, runner: web.AppRunner, site: web.TCPSite) -> None:
        self.runner, self.site = runner, site

    @property
    def port(self) -> int:
        return self.runner.addresses[0][1]

    async def cleanup(self) -> None:
        await self.runner.cleanup()


async def start_api(hub, host: str, port: int) -> ApiServer:
    runner = web.AppRunner(build_app(hub), access_log=None, shutdown_timeout=1.0)     # open event streams must not hold a shutdown up
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return ApiServer(runner, site)
