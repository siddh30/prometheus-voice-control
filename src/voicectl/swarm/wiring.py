from __future__ import annotations

"""Starts this computer's part of the swarm from config: the hub (on one computer) and/or the node (on every computer)."""

import asyncio
import logging
import os
import secrets
import socket

from voicectl.core.config import DATA_DIR, Config
from voicectl.swarm.hub import Hub, Job, join_results
from voicectl.swarm.node import AgentExecutor, SwarmNode

log = logging.getLogger("voicectl.swarm")


def swarm_token(data_dir=DATA_DIR) -> str:
    """The shared secret. Set SWARM_TOKEN in .env on every computer; if it isn't set, one is generated and kept next to the data."""
    if tok := os.environ.get("SWARM_TOKEN", "").strip():
        return tok
    path = data_dir / "swarm_token"
    try:
        return path.read_text().strip()
    except OSError:
        data_dir.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(18)
        path.write_text(tok)
        path.chmod(0o600)
        return tok


def dashboard_url(hub_url: str, token: str) -> str:
    """ws://host:port  ->  http://host:port/?token=...  (the address the Prometheus Swarm app and a phone open)."""
    http = hub_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1).rstrip("/")
    return f"{http}/?token={token}"


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))               # no packet is sent: this only asks the OS which interface it would use
            return s.getsockname()[0]
    except OSError:
        return "localhost"


async def _llm(prompt: str, max_tokens: int = 400) -> str:
    from voicectl.modes.router import anthropic_client

    def go() -> str:
        r = anthropic_client().messages.create(model="claude-haiku-4-5", max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
    return await asyncio.to_thread(go)


async def summarize(job: Job) -> str:
    """One part is its own answer; several parts are woven into a short summary (plain join if the model is unavailable)."""
    parts = [s for s in job.subs if s.result]
    if len(parts) <= 1:
        return await join_results(job)
    listing = "\n\n".join(f"{s.node} ({s.state}): {s.result}" for s in parts)
    return await _llm(f"The user asked: {job.request}\n\nSeveral of their computers worked on it. Combine what they found into a clear answer of "
                      f"two to five sentences, mentioning which computer did what only if it matters.\n\n{listing}")


def start_swarm(config: Config, ctx, primary_runner, bus) -> list[asyncio.Task]:
    """Returns the background tasks started (hub and/or node). Everything is best-effort: the swarm never stops Prometheus itself starting."""
    tasks: list[asyncio.Task] = []
    token = swarm_token()
    port = int(config.get("swarm.port", 8780))
    name = (config.get("swarm.name", "") or socket.gethostname().split(".")[0]).strip()
    loop = asyncio.get_running_loop()
    hub_url = config.get("swarm.hub_url", f"ws://localhost:{port}")
    if not config.get("swarm.hub", False):
        # A node knows where the hub is: write the dashboard address (with the token) where the Prometheus Swarm app looks for it.
        try:
            (DATA_DIR / "swarm_url.txt").write_text(dashboard_url(hub_url, token) + "\n")
        except OSError:
            pass
    if config.get("swarm.hub", False):
        hub = Hub(config.get("swarm.host", "0.0.0.0"), port, token, llm=_llm, summarizer=summarize, default_node=config.get("swarm.default_node", ""))
        tasks.append(loop.create_task(hub.run()))
        url = f"http://{lan_ip()}:{port}/?token={token}"
        try:
            (DATA_DIR / "swarm_url.txt").write_text(url + "\n")
        except OSError:
            pass
        log.info("Prometheus Swarm dashboard: %s  (open it on your phone)", url)
        ctx.shared["swarm_hub"] = hub
    node = SwarmNode(name, hub_url, token, AgentExecutor(config, primary_runner, DATA_DIR), config.get("swarm.description", ""),
                     ["browser", "screen", "files"])
    tasks.append(loop.create_task(node.run()))
    ctx.shared["swarm_node"] = node
    return tasks
