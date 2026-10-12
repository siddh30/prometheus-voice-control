from __future__ import annotations

import logging
import socket

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory
from voicectl.swarm.controller import SwarmController
from voicectl.swarm.executor import AgentExecutor
from voicectl.swarm.link import Link, LocalLink, RemoteLink
from voicectl.swarm.router import PC, Router
from voicectl.swarm.worker import Worker

log = logging.getLogger("voicectl.swarm.settings")


class SwarmConfigError(RuntimeError):
    pass


def build_controller(config: Config, bus: EventBus, token: str, memory: Memory, executor=None) -> SwarmController:
    """The swarm as the settings describe it: this computer (always, in-process) plus every computer under `swarm.pcs`.

        swarm:
          name: Studio                  # this computer
          aliases: [desktop]
          pcs:                          # the others (usually in config.local.yaml)
            - {name: MacBook, host: 192.168.1.20, port: 8781, aliases: [laptop]}
    """
    me = config.get("swarm.name") or socket.gethostname().split(".")[0]
    pcs = [PC(me, tuple(config.get("swarm.aliases", []) or []), local=True)]
    links: dict[str, Link] = {me: LocalLink(me, Worker(me, executor or AgentExecutor(config, memory), stall_after_s=config.get("swarm.stall_after_s", 30)))}
    others = config.get("swarm.pcs", []) or []
    if others and not token:
        raise SwarmConfigError("SWARM_TOKEN is missing in .env, so I can't connect to your other computers.")
    for item in others:
        name = str(item.get("name", "")).strip()
        if not name or name in links:
            raise SwarmConfigError(f"swarm.pcs needs a unique name for every computer (got {name!r}).")
        pcs.append(PC(name, tuple(item.get("aliases", []) or [])))
        links[name] = RemoteLink(name, str(item["host"]), int(item.get("port", 8781)), token,
                                 silent_after_s=config.get("swarm.silent_after_s", 8))
    return SwarmController(links, Router(pcs, model=config.get("swarm.router_model", "claude-haiku-4-5")), bus)
