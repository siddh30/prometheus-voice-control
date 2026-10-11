import asyncio
import json

import pytest
import websockets

from voicectl.swarm.hub import Hub
from voicectl.swarm.node import SwarmNode
from voicectl.swarm.planner import NoNodes, plan

MAC = {"name": "MacBook", "status": "idle", "online": True, "description": "laptop"}
MINI = {"name": "Mac mini", "status": "idle", "online": True, "description": "always on, has my files"}


# ---------------- the planner ----------------
async def test_the_planner_uses_simple_rules_before_any_model():
    assert [a.node for a in await plan("do x", [MAC])] == ["MacBook"]                                  # only one online
    got = await plan("on the mac mini, download the report", [MAC, MINI])
    assert [(a.node, a.task) for a in got] == [("Mac mini", "download the report")]                    # you named it; it knows it is the one
    assert [a.node for a in await plan("check my laptop for updates", [MAC, MINI])] == ["MacBook"]     # aliases
    assert {a.node for a in await plan("run the backup on both", [MAC, MINI])} == {"MacBook", "Mac mini"}
    with pytest.raises(NoNodes):
        await plan("anything", [])
    with pytest.raises(NoNodes):
        await plan("anything", [{**MAC, "online": False}])


async def test_the_planner_model_can_split_work_but_never_invent_computers_and_failure_is_harmless():
    async def llm(prompt):
        return json.dumps({"assignments": [{"node": "Mac mini", "task": "find three jobs", "why": "has the resume"},
                                            {"node": "MacBook", "task": "draft the email", "why": "idle"}, {"node": "Cray-1", "task": "x"}]})
    got = await plan("find me three jobs and also draft a thank you email for the interview", [MAC, MINI], llm)
    assert [(a.node, a.task) for a in got] == [("Mac mini", "find three jobs"), ("MacBook", "draft the email")]   # the invented one is dropped
    async def broken(prompt): raise RuntimeError("api down")
    got = await plan("find me three jobs and also draft a thank you email", [{**MAC, "status": "busy"}, MINI], broken)
    assert [a.node for a in got] == ["Mac mini"]                                                      # fallback: the idle one
    async def junk(prompt): return "not json at all"
    assert len(await plan("find me three jobs and also draft a thank you email", [MAC, MINI], junk, default="Mac mini")) == 1


# ---------------- the hub, with real sockets ----------------
class Peer:
    """A scripted node or dashboard for tests."""
    def __init__(self, ws): self.ws = ws
    async def send(self, **m): await self.ws.send(json.dumps(m))
    async def get(self, kind=None, timeout=3):
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if kind is None or m.get("type") == kind:
                return m
    async def until(self, pred, timeout=3):
        while True:
            m = await self.get(timeout=timeout)
            if pred(m):
                return m


async def start_hub(**kw):
    hub = Hub("127.0.0.1", 0, token="s3cret", **kw)
    await hub.start()
    return hub


async def node_peer(hub, name, token="s3cret"):
    p = Peer(await websockets.connect(f"ws://127.0.0.1:{hub.bound_port}/node?token={token}"))
    await p.send(type="hello", name=name, description=name + " desc", caps=["browser"])
    for _ in range(50):
        if name in hub.nodes:
            return p
        await asyncio.sleep(0.02)
    raise AssertionError("node did not register")


async def ui_peer(hub):
    p = Peer(await websockets.connect(f"ws://127.0.0.1:{hub.bound_port}/ui?token=s3cret"))
    assert (await p.get("snapshot"))["type"] == "snapshot"
    return p


async def test_the_hub_rejects_connections_without_the_token_and_serves_the_dashboard():
    hub = await start_hub()
    try:
        for path in ("/ui", "/node", "/ui?token=wrong"):
            with pytest.raises(Exception):
                await websockets.connect(f"ws://127.0.0.1:{hub.bound_port}{path}")
        import urllib.request
        get = lambda path: asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{hub.bound_port}{path}", timeout=5).read())
        assert "Prometheus Swarm" in (await get("/")).decode()
        assert await get("/health") == b"ok\n"
    finally:
        await hub.stop()


async def test_a_request_is_assigned_run_and_reported_to_the_dashboard():
    hub = await start_hub()
    try:
        mac, mini, ui = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini"), await ui_peer(hub)
        await ui.send(type="submit", text="on the mac mini, download the report")
        task = await mini.get("task")
        assert task["text"] == "download the report"                                    # the MacBook was not involved
        await mini.send(type="step", sub=task["sub"], text="Opening site", done=False)
        step = await ui.until(lambda m: m.get("type") == "sub" and m["sub"]["steps"])
        assert step["sub"]["node"] == "Mac mini" and step["sub"]["state"] == "running"
        await mini.send(type="done", sub=task["sub"], ok=True, result="Saved report.pdf to Downloads")
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["state"] == "done")
        assert job["job"]["result"] == "Saved report.pdf to Downloads"
        assert hub.nodes["Mac mini"].status == "idle"
    finally:
        await hub.stop()


async def test_work_fans_out_to_several_computers_in_parallel_and_the_results_are_joined():
    async def combine(job): return " + ".join(f"{s.node}:{s.result}" for s in job.subs)
    hub = await start_hub(summarizer=combine)
    try:
        mac, mini, ui = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini"), await ui_peer(hub)
        await ui.send(type="submit", text="run the backup on both")
        t1, t2 = await mac.get("task"), await mini.get("task")                           # both started at once
        await mac.send(type="done", sub=t1["sub"], ok=True, result="a")
        await mini.send(type="done", sub=t2["sub"], ok=True, result="b")
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["state"] == "done")
        assert job["job"]["result"] == "MacBook:a + Mac mini:b"
    finally:
        await hub.stop()


async def test_a_busy_computer_queues_work_and_a_computer_that_disappears_fails_its_task_cleanly():
    hub = await start_hub()
    try:
        mini, ui = await node_peer(hub, "Mac mini"), await ui_peer(hub)
        await ui.send(type="submit", text="first thing")
        t1 = await mini.get("task")
        await ui.send(type="submit", text="second thing")
        await asyncio.sleep(0.2)
        assert hub.nodes["Mac mini"].sub == t1["sub"]                                    # the second one waits its turn
        await mini.send(type="done", sub=t1["sub"], ok=True, result="one")
        t2 = await mini.get("task")
        assert t2["text"] == "second thing"
        await mini.ws.close()                                                            # the Mac mini goes to sleep mid-task
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["request"] == "second thing" and m["job"]["state"] != "running")
        assert job["job"]["state"] == "failed" and "went offline" in job["job"]["result"]
        snap = hub.snapshot()
        assert [n["status"] for n in snap["nodes"]] == ["offline"]                       # it still shows on the dashboard, greyed out
        await ui.send(type="submit", text="anything")
        failed = await ui.until(lambda m: m.get("type") == "job" and m["job"]["request"] == "anything")
        assert "can't do that right now" in failed["job"]["result"]
    finally:
        await hub.stop()


async def test_cancel_and_approvals_travel_through_the_hub():
    hub = await start_hub()
    try:
        mini, ui = await node_peer(hub, "Mac mini"), await ui_peer(hub)
        await ui.send(type="submit", text="send the invoice")
        t = await mini.get("task")
        await mini.send(type="approval", sub=t["sub"], text="Send the invoice to the client?")
        asked = await ui.until(lambda m: m.get("type") == "sub" and m["sub"]["approval"])
        await ui.send(type="approve", sub=t["sub"], ok=True)
        assert (await mini.get("approve")) == {"type": "approve", "sub": t["sub"], "ok": True}
        await ui.send(type="cancel", job=asked["sub"]["job"])
        assert (await mini.get("cancel"))["sub"] == t["sub"]
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["state"] == "cancelled")
        assert job["job"]["state"] == "cancelled" and hub.nodes["Mac mini"].status == "idle"
    finally:
        await hub.stop()


# ---------------- the node, end to end ----------------
class FakeExecutor:
    def __init__(self): self.cancelled, self.approved, self.release = False, [], asyncio.Event()
    async def run(self, text, emit):
        await emit("step", text=f"working on {text}", done=False)
        if text == "boom":
            raise RuntimeError("kaboom")
        if text == "wait":
            await self.release.wait()
        await emit("step", text=f"working on {text}", done=True)
        return f"did {text}"
    async def cancel(self): self.cancelled = True
    def approve(self, ok): self.approved.append(ok)


async def test_a_node_runs_tasks_with_its_agent_and_survives_its_own_failures_and_the_hub_restarting():
    hub = await start_hub()
    ex = FakeExecutor()
    node = SwarmNode("Mac mini", f"ws://127.0.0.1:{hub.bound_port}", "s3cret", ex, "always on", retry_s=(0.05, 0.1))
    runner = asyncio.create_task(node.run())
    try:
        await asyncio.wait_for(node.connected.wait(), 3)
        ui = await ui_peer(hub)
        await ui.send(type="submit", text="hello")
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["state"] == "done")
        assert job["job"]["result"] == "did hello"
        await ui.send(type="submit", text="boom")
        job = await ui.until(lambda m: m.get("type") == "job" and m["job"]["request"] == "boom" and m["job"]["state"] != "running")
        assert job["job"]["state"] == "failed" and "kaboom" in job["job"]["result"]
        await ui.send(type="submit", text="again")                                         # the node is still alive after the failure
        assert (await ui.until(lambda m: m.get("type") == "job" and m["job"]["request"] == "again" and m["job"]["state"] == "done"))
        port = hub.bound_port
        await hub.stop()                                                                   # the hub goes away...
        node.connected.clear()
        hub2 = Hub("127.0.0.1", port, token="s3cret")
        await hub2.start()
        for _ in range(100):                                                               # ...and the node rejoins by itself
            if "Mac mini" in hub2.nodes:
                break
            await asyncio.sleep(0.05)
        assert "Mac mini" in hub2.nodes
        await hub2.stop()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await hub.stop()


def test_this_computers_local_settings_override_the_shared_config_but_never_the_dev_profile(tmp_path, monkeypatch):
    from voicectl.core import config as cfgmod
    (tmp_path / "config.yaml").write_text("swarm:\n  enabled: false\n  hub: false\n  port: 8780\nx: 1\n")
    (tmp_path / "config.local.yaml").write_text("swarm:\n  enabled: true\n  hub: true\n")
    (tmp_path / "config.dev.yaml").write_text("swarm:\n  enabled: false\n")
    monkeypatch.setattr(cfgmod, "PROFILE", "stable")
    c = cfgmod.load_config(tmp_path / "config.yaml")
    assert c.get("swarm.enabled") is True and c.get("swarm.hub") is True and c.get("swarm.port") == 8780 and c.get("x") == 1
    monkeypatch.setattr(cfgmod, "PROFILE", "dev")
    assert cfgmod.load_config(tmp_path / "config.yaml").get("swarm.enabled") is False        # dev must never join your real swarm
    real = (cfgmod.ROOT / "config.dev.yaml").read_text()
    assert "swarm:" in real and "enabled: false" in real.split("swarm:")[1]


def test_a_node_writes_the_hub_address_where_the_swarm_app_looks_for_it():
    from voicectl.swarm.wiring import dashboard_url
    assert dashboard_url("ws://my-mac.local:8780", "tok") == "http://my-mac.local:8780/?token=tok"
    assert dashboard_url("wss://hub.example:443/", "t") == "https://hub.example:443/?token=t"


def test_only_one_process_per_computer_runs_the_swarm_and_a_dead_one_is_taken_over(tmp_path):
    import os, subprocess, sys
    from voicectl.swarm import lock
    assert lock.holder(tmp_path) is None and lock.try_acquire(tmp_path)                       # first come, first served
    assert lock.try_acquire(tmp_path)                                                         # re-acquiring from the same process is fine
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "swarm_backend.pid").write_text(str(other.pid))                           # someone else holds it
        assert lock.holder(tmp_path) == other.pid and lock.try_acquire(tmp_path) is False
    finally:
        other.kill(); other.wait()
    assert lock.holder(tmp_path) is None and lock.try_acquire(tmp_path)                       # it died: a stale file is taken over
    (tmp_path / "swarm_backend.pid").write_text("not a number")
    assert lock.holder(tmp_path) is None


def test_the_swarm_backend_entry_point_exists():
    import importlib
    mod = importlib.import_module("voicectl.swarm.service")
    assert callable(mod.main) and callable(mod.serve)
