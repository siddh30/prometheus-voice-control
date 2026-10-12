"""Finding a worker whose address changed. A home router can hand a computer a new address after it restarts, so a saved address
goes stale. The controller asks the local network "which worker is called X?" (one UDP broadcast); the worker answers from its
own address, and the controller connects there. Both sides prove they know SWARM_TOKEN, so strangers on the network get no answer
and a spoofed answer is ignored. Nothing here carries a task: the WebSocket link still does its own authentication."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket

log = logging.getLogger("voicectl.swarm.discovery")

DISCOVERY_PORT = 8782


def _proof(token: str, nonce: str, role: str) -> str:
    return hmac.new(token.encode(), f"{role}:{nonce}".encode(), hashlib.sha256).hexdigest()


class Beacon(asyncio.DatagramProtocol):
    """Worker side: answers a valid "where" probe with this worker's name and WebSocket port."""

    def __init__(self, name: str, token: str, ws_port: int) -> None:
        self.name, self.token, self.ws_port = name, token, ws_port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            probe = json.loads(data)
            nonce = str(probe["nonce"])
            ok = probe.get("type") == "where" and hmac.compare_digest(str(probe.get("proof", "")), _proof(self.token, nonce, "ask"))
        except Exception:
            return
        if ok and self.transport:
            self.transport.sendto(json.dumps({"type": "here", "name": self.name, "port": self.ws_port, "nonce": nonce,
                                              "proof": _proof(self.token, nonce, "answer")}).encode(), addr)

    @classmethod
    async def start(cls, name: str, token: str, ws_port: int, port: int = DISCOVERY_PORT, host: str = "0.0.0.0") -> "Beacon | None":
        """Best effort: a worker that can't open the discovery port still works with a fixed address."""
        beacon = cls(name, token, ws_port)
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: beacon, local_addr=(host, port), reuse_port=True)
        except OSError as e:
            log.warning("discovery is off (%s)", e)
            return None
        return beacon

    def stop(self) -> None:
        if self.transport:
            self.transport.close()


class _Asker(asyncio.DatagramProtocol):
    def __init__(self, token: str, nonce: str, name: str, found: "asyncio.Future[tuple[str, int]]") -> None:
        self.token, self.nonce, self.name, self.found = token, nonce, name.lower(), found

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            a = json.loads(data)
            if (a.get("type") == "here" and a.get("nonce") == self.nonce and str(a.get("name", "")).lower() == self.name
                    and hmac.compare_digest(str(a.get("proof", "")), _proof(self.token, self.nonce, "answer")) and not self.found.done()):
                self.found.set_result((addr[0], int(a["port"])))
        except Exception:
            pass


async def find(name: str, token: str, *, timeout_s: float = 1.5, port: int = DISCOVERY_PORT, broadcast: str = "255.255.255.255") -> tuple[str, int] | None:
    """The (address, port) of the worker called `name` on this network, or None."""
    loop = asyncio.get_running_loop()
    nonce = os.urandom(8).hex()
    found: asyncio.Future = loop.create_future()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(lambda: _Asker(token, nonce, name, found), sock=sock)
    try:
        transport.sendto(json.dumps({"type": "where", "nonce": nonce, "proof": _proof(token, nonce, "ask")}).encode(), (broadcast, port))
        return await asyncio.wait_for(found, timeout_s)
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        transport.close()
