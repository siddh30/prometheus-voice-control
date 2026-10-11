"""Regression: the widget's window must always be as big as the widget (it was once left at its tiny starting size and cut the widget off).

This opens a real floating window for a few seconds, so it only runs on request:  VOICECTL_UI_TESTS=1 pytest tests/test_widget_window.py
(needs the overlay built: cd overlay && swift build -c release)"""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "overlay" / ".build" / "release" / "VoiceOverlay"
pytestmark = pytest.mark.skipif(not os.environ.get("VOICECTL_UI_TESTS") or not BIN.exists(), reason="UI tests run only with VOICECTL_UI_TESTS=1 and a built overlay")


async def test_the_window_grows_and_shrinks_with_the_widget_and_never_clips_it():
    Quartz = pytest.importorskip("Quartz")
    from websockets.asyncio.server import serve

    async def fake(ws):
        send = lambda t, **d: ws.send(json.dumps({"type": t, "data": d}))                # noqa: E731
        await send("state", state="idle", mode="prometheus"); await asyncio.sleep(2.5)
        await send("state", state="listening", mode="prometheus"); await asyncio.sleep(2.5)
        await send("state", state="idle", mode="prometheus"); await asyncio.sleep(30)

    def size(pid):
        ws = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0) if w.get("kCGWindowOwnerPID") == pid]
        return (int(ws[0]["kCGWindowBounds"]["Width"]), int(ws[0]["kCGWindowBounds"]["Height"])) if ws else (0, 0)

    srv = await serve(fake, "127.0.0.1", 8798)
    env = {**os.environ, "VOICECTL_WS": "ws://127.0.0.1:8798"}
    env.pop("VOICECTL_ROOT", None)
    app = subprocess.Popen([str(BIN)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(1.8)
        resting = size(app.pid)
        assert 280 <= resting[0] <= 420 and 50 <= resting[1] <= 90, resting          # the resting pill, not the 260x60 starting placeholder
        await asyncio.sleep(2.5)
        listening = size(app.pid)
        assert listening[0] >= 400 and listening[1] > resting[1] + 60, listening      # the open card is bigger, and nothing is clipped
        await asyncio.sleep(3.0)
        assert size(app.pid) == resting                                               # and it shrinks back to the pill
    finally:
        app.terminate(); app.wait(timeout=5); srv.close()
