"""Run one swarm worker on this computer:  python -m voicectl.swarm   (headless: no UI, no microphone)."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

from voicectl.core.config import load_config
from voicectl.core.memory import shared_memory
from voicectl.swarm.discovery import DISCOVERY_PORT
from voicectl.swarm.executor import AgentExecutor
from voicectl.swarm.server import WorkerServer
from voicectl.swarm.worker import Worker


async def run() -> None:
    config = load_config()
    token = os.environ.get("SWARM_TOKEN", "")
    name = config.get("swarm.worker.name") or socket.gethostname().split(".")[0]
    worker = Worker(name, AgentExecutor(config, shared_memory({})), stall_after_s=config.get("swarm.stall_after_s", 30))
    server = WorkerServer(worker, token, host=config.get("swarm.worker.host", "127.0.0.1"), port=config.get("swarm.worker.port", 8781),
                          heartbeat_s=config.get("swarm.heartbeat_s", 3),
                          discovery_port=config.get("swarm.discovery_port", DISCOVERY_PORT))
    await server.start()
    await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
