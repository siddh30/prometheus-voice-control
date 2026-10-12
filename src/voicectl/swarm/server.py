from __future__ import annotations

import asyncio
import hmac
import json
import logging

from websockets.asyncio.server import ServerConnection, serve

from voicectl.swarm.discovery import Beacon
from voicectl.swarm.protocol import COMMANDS, PROTOCOL_VERSION, validate
from voicectl.swarm.worker import Worker

log = logging.getLogger("voicectl.swarm.server")


class WorkerServer:
    """A worker's network face: authenticated WebSocket connections from controllers. The first message must be `hello` with the
    shared token (SWARM_TOKEN); everything after it goes to the Worker. A heartbeat every few seconds, independent of any task,
    tells the controller this computer is alive even while a task is quiet."""

    def __init__(self, worker: Worker, token: str, host: str = "127.0.0.1", port: int = 8781, heartbeat_s: float = 3.0,
                 discovery_port: int = 0) -> None:
        if not token:
            raise ValueError("SWARM_TOKEN is empty: a worker never listens without a shared secret")
        self.worker, self.token, self.host, self.port, self.heartbeat_s = worker, token, host, port, heartbeat_s
        self.discovery_port = discovery_port                     # 0 = do not answer "where is this worker?" probes
        self._server = None
        self._beacon: Beacon | None = None

    async def _handler(self, ws: ServerConnection) -> None:
        if not await self._authenticate(ws):
            return
        session = self.worker.attach(lambda message: ws.send(json.dumps(message)))
        beat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    await self.worker.command(session, message)
        except Exception:
            log.debug("connection ended", exc_info=True)
        finally:
            beat.cancel()
            await self.worker.detach(session)

    async def _authenticate(self, ws: ServerConnection) -> bool:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            await ws.close()
            return False
        problem = validate(COMMANDS, hello) if isinstance(hello, dict) else "not an object"
        if problem or hello.get("type") != "hello" or not hmac.compare_digest(str(hello.get("token", "")), self.token):
            log.warning("rejected a connection (%s)", problem or "wrong token")
            try:
                await ws.send(json.dumps({"type": "rejected", "reason": "bad token or protocol"}))
            finally:
                await ws.close()
            return False
        await ws.send(json.dumps({"type": "welcome", "name": self.worker.name, "version": PROTOCOL_VERSION}))
        return True

    async def _heartbeat(self, ws: ServerConnection) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                await ws.send('{"type":"heartbeat"}')
        except Exception:
            pass

    async def start(self) -> None:
        self._server = await serve(self._handler, self.host, self.port, max_size=1024 * 1024)
        if self.port == 0:                                       # tests ask for any free port
            self.port = self._server.sockets[0].getsockname()[1]
        if self.discovery_port:
            self._beacon = await Beacon.start(self.worker.name, self.token, self.port, self.discovery_port)
        self.worker.start_monitor()
        log.info("swarm worker %r listening on ws://%s:%d", self.worker.name, self.host, self.port)

    async def stop(self) -> None:
        if self._beacon:
            self._beacon.stop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self.worker.stop()
