"""The stall test. Three computers misbehave at once (one floods, one goes silent, one drops); the controller's event loop must stay
responsive, the widget must receive only a capped trickle of updates, and each problem must be reported as it happens."""
import asyncio
import time

from voicectl.core.events import EventBus
from voicectl.swarm.controller import SwarmController
from voicectl.swarm.link import LinkState, LocalLink
from voicectl.swarm.router import PC, Router
from voicectl.swarm.worker import Worker


class Exec:
    def __init__(self, script): self.script = script
    async def run(self, task): return await self.script(task)
    async def cancel(self, task_id): ...


async def flood(task):
    for i in range(20000):
        task.emit("step", text=f"step {i}", done=i % 2 == 0)
        if i % 500 == 0:
            await asyncio.sleep(0)                    # a real agent yields now and then; the point is the volume
    return "flooded"


async def silent(task):
    task.emit("step", text="started", done=False)
    await asyncio.sleep(30)


async def forever(task):
    task.emit("step", text="working", done=False)
    await asyncio.sleep(30)


async def test_a_flooding_a_silent_and_a_dropped_computer_cannot_starve_the_controller_or_flood_the_widget():
    pcs = [PC("Studio", local=True), PC("Flooder"), PC("Quiet"), PC("Dropper")]
    workers = {"Studio": Worker("Studio", Exec(forever)), "Flooder": Worker("Flooder", Exec(flood)),
               "Quiet": Worker("Quiet", Exec(silent), stall_after_s=0.3), "Dropper": Worker("Dropper", Exec(forever))}
    links = {n: LocalLink(n, w) for n, w in workers.items()}
    bus, snaps = EventBus(), []
    real = bus.publish
    bus.publish = lambda t, **d: (snaps.append((time.monotonic(), d)) if t == "swarm" else None, real(t, **d))[1]
    controller = SwarmController(links, Router(pcs), bus, publish_every_s=0.15)
    await controller.start()
    workers["Quiet"].start_monitor(0.05)

    lag = []
    async def probe():                                            # how late is a 10 ms timer? that is how stuck the loop is
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            lag.append(time.monotonic() - t0 - 0.01)
    watcher = asyncio.create_task(probe())

    run = asyncio.create_task(controller.run("do it on all my computers"))
    await asyncio.sleep(0.4)
    links["Dropper"]._set(LinkState.OFFLINE, "the connection closed")            # one computer drops mid-task
    await asyncio.sleep(1.2)

    states = {p["name"]: p["state"] for p in snaps[-1][1]["pcs"]}
    assert states["Quiet"] == "stalled"                                          # silent: reported, not hidden
    assert states["Dropper"] in ("offline", "failed")                            # dropped: reported
    assert states["Flooder"] in ("done", "working")                              # 20,000 events later, still sane
    assert max(lag) < 0.25, f"the event loop was blocked for {max(lag):.3f}s"
    duration = snaps[-1][0] - snaps[0][0]
    assert len(snaps) <= duration / 0.15 + 6, f"{len(snaps)} widget updates in {duration:.1f}s"      # about 7 a second at most

    run.cancel(); watcher.cancel()
    await asyncio.gather(run, watcher, return_exceptions=True)
    await controller.stop()
