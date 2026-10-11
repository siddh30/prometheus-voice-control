import asyncio
import json

from voicectl.core.events import EventBus
from voicectl.ui_bridge.server import UIBridge


async def test_the_daemon_beats_while_idle_so_the_widget_can_tell_quiet_from_stuck(monkeypatch):
    sent = []

    class WS:
        async def send(self, m): sent.append(json.loads(m))

    class Server:
        connections = [WS()]

    real_sleep = asyncio.sleep
    async def fast(_): await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast)
    bridge = UIBridge(EventBus(), "127.0.0.1", 0)
    bridge._server = Server()
    task = asyncio.create_task(bridge._heartbeat())
    for _ in range(5):
        await real_sleep(0)
    task.cancel()
    assert {"type": "heartbeat", "data": {}} in sent
