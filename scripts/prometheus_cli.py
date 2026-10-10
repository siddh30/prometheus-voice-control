"""Run Prometheus's real brain from text, with no mic, speakers or app: prints what it would SAY and SHOW.
   PYTHONPATH=src .venv/bin/python scripts/prometheus_cli.py "your request"     (uses a hidden test browser)"""
import asyncio, copy, sys, time

from voicectl.agents.runner import AgentRunner, Finished, TextDelta, TextEnd, TextStart
from voicectl.core.config import Config, DATA_DIR, load_config
from voicectl.core.events import EventBus
from voicectl.core.memory import Memory


async def main(request: str) -> None:
    raw = copy.deepcopy(load_config().raw)
    raw.setdefault("browser", {}).update({"backend": "playwright", "headless": True})
    cfg, bus = Config(raw), EventBus()
    runner = AgentRunner(cfg, bus, Memory(DATA_DIR / "cli.db"), prompt_name="prometheus_prompt.txt")
    runner.mode_name = "prometheus"
    t0 = time.monotonic()

    async def show():
        async for ev in bus.subscribe():
            if ev.type in ("step", "checklist", "approval", "error"):
                print(f"   [{time.monotonic() - t0:5.1f}s] {ev.type}: {ev.data}")
    watcher = asyncio.create_task(show())
    blocks: dict[int, str] = {}
    async for ev in runner.stream_turn(request):
        if isinstance(ev, TextStart): blocks[ev.id] = ""
        elif isinstance(ev, TextDelta):
            if not blocks[ev.id]: print(f"   [{time.monotonic() - t0:5.1f}s] SAYS (first word): ", end="")
            blocks[ev.id] += ev.text
        elif isinstance(ev, TextEnd): print(f"{blocks[ev.id].strip()!r}")
        elif isinstance(ev, Finished): print(f"   [{time.monotonic() - t0:5.1f}s] DONE")
    await asyncio.sleep(0.2); watcher.cancel()
    await runner.close()

asyncio.run(main(" ".join(sys.argv[1:])))
