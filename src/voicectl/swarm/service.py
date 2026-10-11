from __future__ import annotations

"""`python -m voicectl.swarm.service`: a HEADLESS swarm node, for a computer you never open Voice Control on (`scripts/swarm_node.sh install`
runs it at login). It is this computer's node (and the hub, when config says so), with no microphone, hotkeys, voice or window.

If Voice Control is already running the swarm here, this waits quietly and takes over if that one quits."""

import asyncio
import logging
import os
import signal
import sys
from types import SimpleNamespace

from voicectl.core.config import DATA_DIR, load_config
from voicectl.swarm import lock
from voicectl.swarm.wiring import start_swarm

log = logging.getLogger("voicectl.swarm.service")


async def serve() -> None:
    config = load_config()
    while not lock.try_acquire(DATA_DIR):
        log.info("the swarm is already running here (pid %s); waiting", lock.holder(DATA_DIR))
        await asyncio.sleep(20)
    ctx = SimpleNamespace(shared={})
    tasks = start_swarm(config, ctx, lambda: None, None)
    log.info("Prometheus Swarm backend is up")
    await asyncio.gather(*tasks)


def main() -> None:
    try:
        os.nice(8)                                         # a background node never competes with the desktop
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    loop = asyncio.new_event_loop()
    main_task = loop.create_task(serve())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
