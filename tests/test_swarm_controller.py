import asyncio

from voicectl.core.events import EventBus
from voicectl.swarm.controller import SwarmController
from voicectl.swarm.link import LinkState, LocalLink, RemoteLink
from voicectl.swarm.router import PC, Router
from voicectl.swarm.server import WorkerServer
from voicectl.swarm.worker import Worker


class Exec:
    def __init__(self, script): self.script, self.cancelled = script, []
    async def run(self, task): return await self.script(task)
    async def cancel(self, task_id): self.cancelled.append(task_id)


async def quick(task):
    task.emit("checklist", items=[{"text": task.text, "status": "in_progress"}])
    task.emit("step", text=f"doing {task.text}", done=False)
    task.emit("step", text=f"doing {task.text}", done=True)
    return f"done: {task.text}"


def build(scripts, local="Studio", stall_after_s=30.0):
    pcs = [PC(name, local=(name == local)) for name in scripts]
    workers = {n: Worker(n, Exec(s), stall_after_s=stall_after_s) for n, s in scripts.items()}
    links = {n: LocalLink(n, w) for n, w in workers.items()}
    bus, snaps = EventBus(), []
    real = bus.publish
    bus.publish = lambda t, **d: (snaps.append(d) if t == "swarm" else None, real(t, **d))[1]
    return SwarmController(links, Router(pcs), bus, publish_every_s=0.02), workers, links, snaps


def pc(snap, name): return next(p for p in snap["pcs"] if p["name"] == name)


async def until(predicate, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_request_runs_on_this_computer_and_the_board_shows_the_tick_marks():
    c, workers, links, snaps = build({"Studio": quick, "MacBook": quick})
    await c.start()
    out = await asyncio.wait_for(c.run("open linkedin"), 3)
    assert out == "Studio: done: open linkedin"
    await asyncio.sleep(0.1)
    last = snaps[-1]
    assert pc(last, "Studio")["state"] == "done" and pc(last, "Studio")["checklist"] == [{"text": "open linkedin", "status": "done"}]
    assert pc(last, "MacBook")["state"] == "online" or pc(last, "MacBook")["state"] == "idle"        # untouched
    assert last["routing"] == "default"
    await c.stop()


async def test_two_computers_work_at_the_same_time_each_on_its_own_task():
    gate = asyncio.Event()
    async def slow(task):
        task.emit("step", text=f"working on {task.text}", done=False)
        await gate.wait()
        return f"finished {task.text}"
    c, workers, links, snaps = build({"Studio": quick, "MacBook": slow, "Mac mini": slow})
    await c.start()
    run = asyncio.create_task(c.run("open gmail on the macbook and play music on the mac mini"))
    assert await until(lambda: snaps and pc(snaps[-1], "MacBook")["state"] == "working" and pc(snaps[-1], "Mac mini")["state"] == "working")
    assert pc(snaps[-1], "MacBook")["task"] == "open gmail" and pc(snaps[-1], "Mac mini")["task"] == "play music"
    assert pc(snaps[-1], "Studio")["state"] != "working"                                            # this computer was not involved
    gate.set()
    out = await asyncio.wait_for(run, 3)
    assert "MacBook: finished open gmail" in out and "Mac mini: finished play music" in out
    await c.stop()


async def test_a_computer_that_is_offline_fails_its_part_and_the_others_still_finish():
    c, workers, links, snaps = build({"Studio": quick, "MacBook": quick})
    links["MacBook"].state = LinkState.OFFLINE
    links["MacBook"].detail = "can't reach 192.168.1.20:8781"
    await c.start()
    out = await asyncio.wait_for(c.run("update chrome on all my computers"), 3)       # "all" skips offline computers
    assert "Studio: done" in out and "MacBook" not in out
    out2 = await asyncio.wait_for(c.run("open gmail on the macbook"), 3)              # named but offline: said so, plainly
    await asyncio.sleep(0.1)
    assert pc(snaps[-1], "MacBook")["state"] == "offline" and "offline" in out2
    await c.stop()


async def test_f10_while_working_cancels_the_work_on_every_computer_and_clears_the_cards():
    async def forever(task):
        task.emit("step", text="working", done=False)
        await asyncio.sleep(30)
    c, workers, links, snaps = build({"Studio": forever, "MacBook": forever})
    await c.start()
    run = asyncio.create_task(c.run("search the web on both"))
    assert await until(lambda: snaps and all(pc(snaps[-1], n)["state"] == "working" for n in ("Studio", "MacBook")))
    run.cancel()                                                                       # what pressing Ctrl+F10 does to the turn
    await asyncio.gather(run, return_exceptions=True)
    assert workers["Studio"].executor.cancelled and workers["MacBook"].executor.cancelled
    await asyncio.sleep(0.1)
    assert all(pc(snaps[-1], n)["state"] != "working" for n in ("Studio", "MacBook"))
    await c.stop()


async def test_a_new_request_replaces_the_running_one():
    gate = asyncio.Event()
    async def first(task):
        if task.text == "slow job":
            await gate.wait()
        return f"finished {task.text}"
    c, workers, links, snaps = build({"Studio": first})
    await c.start()
    a = asyncio.create_task(c.run("slow job"))
    assert await until(lambda: snaps and pc(snaps[-1], "Studio")["state"] == "working")
    out = await asyncio.wait_for(c.run("quick job"), 3)
    assert out == "Studio: finished quick job" and workers["Studio"].executor.cancelled
    a.cancel(); await asyncio.gather(a, return_exceptions=True)
    await c.stop()


async def test_a_silent_task_shows_as_stalled_and_a_computer_that_stops_answering_as_not_responding():
    gate = asyncio.Event()
    async def quiet(task):
        task.emit("step", text="searching", done=False)
        await gate.wait()
        return "found it"
    c, workers, links, snaps = build({"Studio": quiet}, stall_after_s=0.2)
    await c.start()
    workers["Studio"].start_monitor(0.05)
    run = asyncio.create_task(c.run("search"))
    assert await until(lambda: snaps and pc(snaps[-1], "Studio")["state"] == "stalled")
    links["Studio"]._set(LinkState.NOT_RESPONDING, "no word from that computer")
    assert await until(lambda: pc(snaps[-1], "Studio")["state"] == "not_responding")        # the connection problem wins on screen
    links["Studio"]._set(LinkState.ONLINE)
    gate.set()
    assert await asyncio.wait_for(run, 3) == "Studio: found it"
    await c.stop()


async def test_an_approval_waits_for_a_yes_and_work_resumes():
    async def needs_ok(task):
        ok = await task.ask_approval("Send this email?")
        task.emit("step", text="sent" if ok else "not sent", done=True)
        return "sent" if ok else "not sent"
    c, workers, links, snaps = build({"Studio": needs_ok})
    await c.start()
    run = asyncio.create_task(c.run("email bob"))
    assert await until(lambda: c.pending_approval == "Studio: Send this email?")
    assert pc(snaps[-1] if snaps else {"pcs": []}, "Studio")["state"] in ("waiting", "working") if snaps else True
    await c.approve(True)
    assert await asyncio.wait_for(run, 3) == "Studio: sent"
    await c.stop()


async def test_a_flood_of_worker_events_becomes_a_few_widget_updates():
    async def chatty(task):
        for i in range(500):
            task.emit("step", text=f"step {i}", done=False)
        return "done"
    c, workers, links, snaps = build({"Studio": chatty})
    c.publish_every_s = 0.1
    await c.start()
    await asyncio.wait_for(c.run("go"), 5)
    await asyncio.sleep(0.2)
    assert len(snaps) <= 12                                                           # 500+ events, a handful of updates
    await c.stop()


async def test_the_pick_from_the_widget_sends_everything_to_that_computer():
    c, workers, links, snaps = build({"Studio": quick, "MacBook": quick})
    await c.start()
    c.pick("MacBook")
    out = await asyncio.wait_for(c.run("open gmail"), 3)
    assert out == "MacBook: done: open gmail"
    c.pick("auto")
    assert (await asyncio.wait_for(c.run("open gmail"), 3)).startswith("Studio")
    await c.stop()


async def test_the_same_controller_works_over_a_real_connection_to_a_remote_worker():
    server = WorkerServer(Worker("MacBook", Exec(quick)), "tok", port=0, heartbeat_s=0.1)
    await server.start()
    pcs = [PC("Studio", local=True), PC("MacBook")]
    links = {"Studio": LocalLink("Studio", Worker("Studio", Exec(quick))), "MacBook": RemoteLink("MacBook", "127.0.0.1", server.port, "tok", retry_s=0.05)}
    bus, snaps = EventBus(), []
    real = bus.publish
    bus.publish = lambda t, **d: (snaps.append(d) if t == "swarm" else None, real(t, **d))[1]
    c = SwarmController(links, Router(pcs), bus, publish_every_s=0.02)
    await c.start()
    assert await until(lambda: links["MacBook"].state is LinkState.ONLINE)
    out = await asyncio.wait_for(c.run("check the news on the macbook"), 3)
    assert out == "MacBook: done: check the news"
    await c.stop(); await server.stop()


async def test_a_computer_not_in_the_new_request_does_not_keep_the_last_task_on_its_card():
    c, workers, links, snaps = build({"Studio": quick, "MacBook": quick})
    await c.start()
    await asyncio.wait_for(c.run("close all tabs on the macbook"), 3)
    await asyncio.wait_for(c.run("do the same on this mac"), 3)
    await asyncio.sleep(0.1)
    assert pc(snaps[-1], "MacBook")["task"] == "" and pc(snaps[-1], "Studio")["task"] != ""
    await c.stop()


async def test_a_worker_can_pass_work_on_mid_task_in_plain_words_and_the_request_waits_for_it():
    seen = {}
    async def studio(task):
        seen["studio"] = task.context
        return "Found the link: example.com/report.\nHANDOFF MacBook: open example.com/report and read me the title"
    async def macbook(task):
        seen["macbook"] = task.context
        return "The title is Q3 Report."
    c, workers, links, snaps = build({"Studio": studio, "MacBook": macbook})
    await c.start()
    out = await asyncio.wait_for(c.run("find the report link"), 3)
    await asyncio.sleep(0.1)
    assert "Studio: Found the link: example.com/report." in out and "MacBook: The title is Q3 Report." in out
    assert "HANDOFF" not in out                                                        # the handover line is not shown as an answer
    assert "<Swarm>" in seen["studio"] and "MacBook" in seen["studio"]                 # the worker was told about the other computers
    assert '<HandedOver from="Studio">' in seen["macbook"] and "example.com/report" in seen["macbook"]
    last = snaps[-1]
    assert pc(last, "Studio")["to"] == "MacBook" and pc(last, "MacBook")["from"] == "Studio"
    assert pc(last, "MacBook")["task"] == "open example.com/report and read me the title"
    await c.stop()


async def test_a_handover_loop_is_cut_and_an_unknown_computer_is_ignored():
    async def studio(task): return "ok\nHANDOFF MacBook: do the thing\nHANDOFF Nowhere: do it there"
    async def macbook(task): return "done\nHANDOFF Studio: do the thing\nHANDOFF MacBook: do the thing"
    c, workers, links, snaps = build({"Studio": studio, "MacBook": macbook})
    await c.start()
    out = await asyncio.wait_for(c.run("start"), 3)
    assert "Studio" in out and "MacBook" in out and "Nowhere" not in out
    await c.stop()
