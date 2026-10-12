import asyncio

from voicectl.swarm import discovery
from voicectl.swarm.link import LinkState, RemoteLink

TOKEN = "s3cret-token"
LOCAL = "127.0.0.1"


async def free_port():
    loop = asyncio.get_running_loop()
    t, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=(LOCAL, 0))
    port = t.get_extra_info("sockname")[1]
    t.close()
    await asyncio.sleep(0.05)                                  # let the socket really close
    return port


async def test_a_worker_answers_a_probe_with_its_address_and_port():
    port = await free_port()
    beacon = await discovery.Beacon.start("MacBookWorker", TOKEN, 8781, port, LOCAL)
    try:
        assert await discovery.find("macbookworker", TOKEN, port=port, broadcast=LOCAL) == (LOCAL, 8781)
    finally:
        beacon.stop()


async def test_strangers_and_other_names_get_nothing():
    port = await free_port()
    beacon = await discovery.Beacon.start("MacBookWorker", TOKEN, 8781, port, LOCAL)
    try:
        assert await discovery.find("macbookworker", "wrong-token", timeout_s=0.4, port=port, broadcast=LOCAL) is None
        assert await discovery.find("someone-else", TOKEN, timeout_s=0.4, port=port, broadcast=LOCAL) is None
    finally:
        beacon.stop()


async def test_no_worker_means_no_answer_not_an_error():
    assert await discovery.find("nobody", TOKEN, timeout_s=0.3, port=await free_port(), broadcast=LOCAL) is None


async def test_a_link_follows_its_worker_to_a_new_address():
    from voicectl.swarm.server import WorkerServer
    from voicectl.swarm.worker import Worker

    class Quick:
        async def run(self, task): return "ok"
        async def cancel(self): pass

    server = WorkerServer(Worker("MacBookWorker", Quick()), TOKEN, host=LOCAL, port=0)
    await server.start()
    async def locate(): return (LOCAL, server.port)
    link = RemoteLink("MacBook", LOCAL, 1, TOKEN, connect_timeout_s=0.5, retry_s=0.05, locate=locate)    # the saved port is wrong
    await link.start()
    for _ in range(100):
        if link.state is LinkState.ONLINE:
            break
        await asyncio.sleep(0.05)
    assert link.state is LinkState.ONLINE and link.port == server.port
    await link.stop()
    await server.stop()
