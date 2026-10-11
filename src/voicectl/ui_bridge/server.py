from __future__ import annotations

import json
import logging

from websockets.asyncio.server import ServerConnection, serve

from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.ui")


class UIBridge:
    """Streams every bus event to connected overlay clients as JSON; client messages come back
    on the bus as `ui_command` events (approve/deny, etc.). A new client gets the latest state."""

    def __init__(self, bus: EventBus, host: str, port: int, hello: dict | None = None, snapshot=None) -> None:
        self.bus, self.host, self.port = bus, host, port
        self.hello = hello
        self.snapshot = snapshot or (lambda: [])        # extra state messages for a new client, e.g. whether voice is on
        self._last_state: dict = {"type": "state", "data": {"state": "idle", "mode": None}}

    async def _handler(self, ws: ServerConnection) -> None:
        if self.hello:
            await ws.send(json.dumps({"type": "config", "data": self.hello}))
        await ws.send(json.dumps(self._last_state))
        for msg in self.snapshot():
            await ws.send(json.dumps(msg))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.bus.publish("ui_command", command=msg.get("type"), **{k: v for k, v in msg.items() if k != "type"})

    async def _pump(self) -> None:
        async for ev in self.bus.subscribe():
            payload = {"type": ev.type, "data": ev.data}
            if ev.type == "state":
                self._last_state = payload
            self._broadcast_text = json.dumps(payload)
            for ws in list(self._server.connections):
                try:
                    await ws.send(self._broadcast_text)
                except Exception:
                    pass

    async def _heartbeat(self) -> None:
        """A beat every 3 s tells the widget the daemon is alive even when nothing else is happening; if it stops, the widget says so."""
        import asyncio
        while True:
            await asyncio.sleep(3)
            for ws in list(self._server.connections):
                try:
                    await ws.send('{"type":"heartbeat","data":{}}')
                except Exception:
                    pass

    async def run(self) -> None:
        import asyncio
        async with serve(self._handler, self.host, self.port, max_size=32 * 1024 * 1024) as server:   # screenshots travel this way
            self._server = server
            log.info("UI bridge on ws://%s:%d", self.host, self.port)
            beat = asyncio.create_task(self._heartbeat())
            try:
                await self._pump()
            finally:
                beat.cancel()
