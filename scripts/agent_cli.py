"""Run Agent mode from text, no mic/hotkeys: `python scripts/agent_cli.py "your request"`. Prints bus events."""
import asyncio, sys
from voicectl.core.config import DATA_DIR, load_config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory
from voicectl.agents.runner import AgentRunner


async def main(text: str) -> None:
    cfg, bus = load_config(), EventBus()
    runner = AgentRunner(cfg, bus, Memory(DATA_DIR / "voicectl.db"))

    async def watch():
        async for ev in bus.subscribe():
            if ev.type == "level":
                continue
            print(f"  [{ev.type}] {ev.data}")
            if ev.type == "approval":  # headless test: approve via stdin
                ok = input("  approve? [y/N] ").strip().lower() == "y"
                runner.on_ui_command({"command": "approval", "approved": ok})
    w = asyncio.create_task(watch())
    try:
        print("RESULT:", await runner.run_turn(text))
    finally:
        w.cancel()
        await runner.close()

asyncio.run(main(" ".join(sys.argv[1:])))
