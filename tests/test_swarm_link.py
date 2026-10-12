import asyncio

import pytest

from voicectl.swarm.link import LinkState, LinkUnavailable, LocalLink, RemoteLink
from voicectl.swarm.server import WorkerServer
from voicectl.swarm.worker import Worker

TOKEN = "s3cret-token"


class Quick:
    async def run(self, task):
        task.emit("step", text=f"did {task.text}", done=True)
        return f"finished {task.text}"
    async def cancel(self, task_id): ...


class Hangs:
    async def run(self, task):
        task.emit("step", text="started", done=False)
        await asyncio.sleep(30)
    async def cancel(self, task_id): ...


async def until(predicate, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def serve(executor=None, token=TOKEN, heartbeat_s=0.1, port=0):
    server = WorkerServer(Worker("macbook", executor or Quick()), token, port=port, heartbeat_s=heartbeat_s)
    await server.start()
    return server


def link_to(server, token=TOKEN, **kw):
    states, events = [], []
    link = RemoteLink("macbook", "127.0.0.1", server.port, token, retry_s=0.05, **kw)
    link.on_state = lambda pc, st: states.append(st)
    link.on_event = lambda pc, ev: events.append(ev)
    return link, states, events


async def test_a_task_goes_over_the_wire_and_its_progress_comes_back_without_heartbeats():
    server = await serve()
    link, states, events = link_to(server)
    await link.start()
    assert await until(lambda: link.state is LinkState.ONLINE)
    await link.send_task("t1", "open gmail")
    assert await until(lambda: any(e["type"] == "done" for e in events))
    assert [e["type"] for e in events] == ["started", "step", "result", "done"]       # heartbeats keep the link fresh but are not news
    assert events[2]["text"] == "finished open gmail"
    await link.stop(); await server.stop()


async def test_a_wrong_token_is_refused_and_the_reason_is_plain():
    server = await serve()
    link, states, events = link_to(server, token="not-the-token")
    await link.start()
    assert await until(lambda: link.state is LinkState.OFFLINE and "token" in link.detail)
    with pytest.raises(LinkUnavailable):
        await link.send_task("t1", "x")
    await link.stop(); await server.stop()


async def test_a_worker_never_starts_without_a_secret():
    with pytest.raises(ValueError):
        WorkerServer(Worker("x", Quick()), "")


async def test_a_dropped_connection_shows_offline_and_comes_back_by_itself():
    server = await serve()
    port = server.port
    link, states, events = link_to(server)
    await link.start()
    assert await until(lambda: link.state is LinkState.ONLINE)
    await server.stop()                                                      # the other computer goes away
    assert await until(lambda: link.state is LinkState.OFFLINE)
    server2 = await serve(port=port)                                         # ...and returns
    assert await until(lambda: link.state is LinkState.ONLINE, timeout=5)
    assert LinkState.OFFLINE in states and states[-1] is LinkState.ONLINE
    await link.stop(); await server2.stop()


async def test_a_worker_that_goes_quiet_is_reported_not_responding_by_the_controllers_own_watchdog():
    server = await serve(heartbeat_s=60)                                     # alive, but says nothing
    link, states, events = link_to(server, silent_after_s=0.3)
    await link.start()
    assert await until(lambda: link.state is LinkState.ONLINE)
    assert await until(lambda: link.state is LinkState.NOT_RESPONDING)
    await link.send_task("t1", "wake up")                                    # it speaks again: back to online
    assert await until(lambda: link.state is LinkState.ONLINE)
    await link.stop(); await server.stop()


async def test_the_local_link_behaves_like_a_remote_one():
    worker = Worker("this-mac", Quick())
    link = LocalLink("this-mac", worker)
    events = []
    link.on_event = lambda pc, ev: events.append(ev["type"])
    await link.start()
    await link.send_task("t1", "hello")
    assert await until(lambda: "done" in events)
    assert events == ["started", "step", "result", "done"] and link.state is LinkState.ONLINE
    await link.stop()


async def test_cancel_and_a_dropped_controller_stop_remote_work():
    server = await serve(Hangs())
    link, states, events = link_to(server)
    await link.start()
    assert await until(lambda: link.state is LinkState.ONLINE)
    await link.send_task("t1", "long")
    assert await until(lambda: any(e["type"] == "step" for e in events))
    await link.cancel("t1")
    assert await until(lambda: any(e["type"] == "failed" and e["error"] == "cancelled" for e in events))
    await link.send_task("t2", "long again")
    assert await until(lambda: server.worker._running)
    await link.stop()                                                        # the controller disappears
    assert await until(lambda: not server.worker._running)                   # its task does not keep running for nobody
    await server.stop()
