"""Plays a scripted event sequence to the overlay (for UI development without mic/STT)."""
import asyncio, json, math, sys
from websockets.asyncio.server import serve

async def send(ws, type, **data):
    await ws.send(json.dumps({"type": type, "data": data}))

async def handler(ws):
    await asyncio.sleep(1.0)
    await send(ws, "state", state="listening", mode="dictate")
    for i in range(60):
        await send(ws, "level", value=0.5 + 0.45 * math.sin(i / 3) * math.sin(i / 7))
        await asyncio.sleep(1 / 30)
    await send(ws, "state", state="transcribing", mode="dictate")
    await asyncio.sleep(2.5)
    await send(ws, "transcript", text="Open my resume and search for product manager jobs", mode="dictate")
    await send(ws, "state", state="typing", mode="dictate")
    await asyncio.sleep(3)
    await send(ws, "state", state="idle", mode="dictate")
    await asyncio.sleep(1)
    # agent run
    await send(ws, "state", state="listening", mode="agent")
    for i in range(40):
        await send(ws, "level", value=0.4 + 0.4 * math.sin(i / 2)); await asyncio.sleep(1 / 30)
    await send(ws, "state", state="transcribing", mode="agent"); await asyncio.sleep(1)
    await send(ws, "transcript", text="Find me product manager jobs on LinkedIn that fit my resume", mode="agent")
    await send(ws, "state", state="thinking", mode="agent")
    await send(ws, "session", label="SESSION · TURN 2 · 4 MIN")
    await send(ws, "plan", steps=["Read resume from ~/Documents", "Search LinkedIn for PM roles", "Rank by fit and summarize"])
    await asyncio.sleep(4)
    await send(ws, "state", state="acting", mode="agent")
    await send(ws, "step", text="Reading resume.pdf", done=True)
    await send(ws, "step", text="Searching LinkedIn: product manager", done=False)
    await asyncio.sleep(3)
    await send(ws, "approval", text="Click 'Easy Apply' on Stripe — Senior PM?")
    await asyncio.sleep(60)

async def main():
    async with serve(handler, "127.0.0.1", 8765):
        await asyncio.sleep(120)
asyncio.run(main())
