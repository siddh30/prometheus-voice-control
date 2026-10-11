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


# ---------------- the HTTP API and the voice assistant's tools ----------------
import aiohttp


async def api_hub(**kw):
    hub = Hub("127.0.0.1", 0, token="s3cret", **kw)
    await hub.start(api_port=0)
    return hub, f"http://127.0.0.1:{hub.api.port}"


H = {"Authorization": "Bearer s3cret"}


async def test_the_api_needs_the_token_lists_computers_runs_a_pinned_job_and_waits_for_it():
    hub, base = await api_hub()
    try:
        mac, mini = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini")
        async with aiohttp.ClientSession() as s:
            assert (await s.get(base + "/api/health")).status == 200                          # health is open; everything else is not
            assert (await s.get(base + "/api/computers")).status == 401
            assert (await s.get(base + "/api/computers", headers={"Authorization": "Bearer wrong"})).status == 401
            rows = await (await s.get(base + "/api/computers", headers=H)).json()
            assert {r["name"] for r in rows} == {"MacBook", "Mac mini"}
            r = await s.post(base + "/api/jobs", json={"text": "download the report", "computer": "mini"}, headers=H)   # a loose name finds the Mac mini
            job = await r.json()
            assert r.status == 201 and job["subs"][0]["node"] == "Mac mini"
            task = await mini.get("task")
            assert task["text"] == "download the report" and mac.ws.state.name == "OPEN"
            waiter = asyncio.create_task(s.get(f"{base}/api/jobs/{job['id']}/wait?timeout=5", headers=H))
            await asyncio.sleep(0.1)
            await mini.send(type="done", sub=task["sub"], ok=True, result="saved")
            done = await (await waiter).json()
            assert done["state"] == "done" and done["subs"][0]["result"] == "saved"
            bad = await s.post(base + "/api/jobs", json={"text": "x", "computer": "Cray-1"}, headers=H)
            assert (await bad.json())["state"] == "failed"                                      # a computer that isn't there is a clear failure
            assert (await s.post(base + "/api/jobs", json={}, headers=H)).status == 400
            assert (await s.get(base + "/api/jobs/nope", headers=H)).status == 404
    finally:
        await hub.stop()


async def test_a_wait_returns_early_when_a_computer_needs_approval_and_the_answer_goes_back():
    hub, base = await api_hub()
    try:
        mini = await node_peer(hub, "Mac mini")
        async with aiohttp.ClientSession(headers=H) as s:
            job = await (await s.post(base + "/api/jobs", json={"text": "send the invoice"})).json()
            task = await mini.get("task")
            await mini.send(type="approval", sub=task["sub"], text="Send it to the client?")
            asked = await (await s.get(f"{base}/api/jobs/{job['id']}/wait?timeout=5")).json()
            assert asked["state"] == "running" and asked["subs"][0]["approval"]                  # did not sit there for 5 s
            await s.post(f"{base}/api/subs/{task['sub']}/approve", json={"ok": True})
            assert (await mini.get("approve"))["ok"] is True
            r = await s.post(f"{base}/api/jobs/{job['id']}/cancel")
            assert (await r.json())["state"] == "cancelled"
    finally:
        await hub.stop()


async def test_the_event_stream_starts_with_the_current_state_then_follows_changes():
    hub, base = await api_hub()
    try:
        mini = await node_peer(hub, "Mac mini")
        async with aiohttp.ClientSession(headers=H) as s:
            async with s.get(base + "/api/events") as r:
                first = (await asyncio.wait_for(r.content.readline(), 3)).decode()
                assert first.startswith("data: ") and "snapshot" in first and "Mac mini" in first
                await s.post(base + "/api/jobs", json={"text": "hi"})
                seen = ""
                for _ in range(10):
                    line = (await asyncio.wait_for(r.content.readline(), 3)).decode()
                    seen += line
                    if '"job"' in line:
                        break
                assert '"type": "job"' in seen
    finally:
        await hub.stop()


async def test_the_voice_tools_send_work_to_the_swarm_and_report_back_and_fail_politely():
    from voicectl.core.events import EventBus
    from voicectl.swarm.tools import build_swarm_tools, format_job
    hub, base = await api_hub()
    try:
        mini = await node_peer(hub, "Mac mini")
        bus = EventBus()
        steps = []
        async def watch():
            async for ev in bus.subscribe():
                if ev.type == "step": steps.append(ev.data["text"])
        w = asyncio.create_task(watch()); await asyncio.sleep(0)
        tools = {t.name: t for t in build_swarm_tools(base, "s3cret", bus)}
        assert set(tools) == {"swarm_computers", "swarm_run", "swarm_wait", "swarm_status", "swarm_approve", "swarm_cancel"}
        listing = await tools["swarm_computers"].handler({})
        assert "Mac mini: idle" in listing["content"][0]["text"]
        run = asyncio.create_task(tools["swarm_run"].handler({"task": "find three roles", "computer": "Mac mini"}))
        task = await mini.get("task")
        await mini.send(type="step", sub=task["sub"], text="Opening apple.com/careers", done=False)
        await asyncio.sleep(0.3)
        await mini.send(type="done", sub=task["sub"], ok=True, result="Found 3 roles")
        out = await asyncio.wait_for(run, 10)
        assert out["content"][0]["text"] == "Found 3 roles" and not out.get("is_error")
        assert "Mac mini: Opening apple.com/careers" in steps                                  # progress shows on this Mac's widget too
        bad = build_swarm_tools("http://127.0.0.1:9", "s3cret")                                 # nothing is listening there
        out = await bad[0].handler({})
        assert out["is_error"] and "can't reach the swarm hub" in out["content"][0]["text"]
        w.cancel()
    finally:
        await hub.stop()
    assert format_job({"state": "done", "result": "", "subs": [{"node": "A", "state": "done", "result": "x"}, {"node": "B", "state": "failed", "result": "y"}]}) == "A (done): x\nB (failed): y"


async def test_an_agent_that_is_itself_a_swarm_task_gets_no_swarm_tools_so_work_cannot_bounce_between_computers(tmp_path):
    from voicectl.agents.runner import AgentRunner
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory
    cfg = Config({"swarm": {"enabled": True, "hub_url": "ws://localhost:8780"}, "browser": {"backend": "playwright"}})
    assert "swarm" in AgentRunner(cfg, EventBus(), Memory(tmp_path / "a.db"))._swarm_server()
    assert AgentRunner(cfg, EventBus(), Memory(tmp_path / "b.db"), swarm_tools=False)._swarm_server() == {}
    off = Config({"swarm": {"enabled": False}, "browser": {"backend": "playwright"}})
    assert AgentRunner(off, EventBus(), Memory(tmp_path / "c.db"))._swarm_server() == {}


async def test_parallel_work_is_reported_as_each_computer_finishes_not_all_at_the_end():
    import re
    from voicectl.swarm.tools import build_swarm_tools
    hub, base = await api_hub()
    try:
        mac, mini = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini")
        tools = {t.name: t for t in build_swarm_tools(base, "s3cret")}
        run = asyncio.create_task(tools["swarm_run"].handler({"task": "run the backup on both", "computer": ""}))
        t1, t2 = await mac.get("task"), await mini.get("task")                                      # both started at once, in parallel
        await mac.send(type="done", sub=t1["sub"], ok=True, result="MacBook backup finished")
        first = (await asyncio.wait_for(run, 10))["content"][0]["text"]                              # returns at the FIRST one, not at the end
        assert "MacBook finished: MacBook backup finished" in first and "Still working: Mac mini" in first and "swarm_wait" in first
        job = re.search(r"job (\w+)", first).group(1)
        assert hub.jobs[job].state == "running"                                                      # the other computer is still going
        nxt = asyncio.create_task(tools["swarm_wait"].handler({"job": job}))
        await asyncio.sleep(0.2)
        await mini.send(type="done", sub=t2["sub"], ok=True, result="Mac mini backup finished")
        last = (await asyncio.wait_for(nxt, 10))["content"][0]["text"]
        assert "MacBook" in last and "Mac mini" in last and hub.jobs[job].state == "done"            # then the combined result
    finally:
        await hub.stop()


async def node_peer_t(hub, name):
    p = Peer(await websockets.connect(f"ws://127.0.0.1:{hub.bound_port}/node?token=t"))
    await p.send(type="hello", name=name, description="x", caps=[])
    for _ in range(50):
        if name in hub.nodes: return p
        await asyncio.sleep(0.02)
    raise AssertionError("node did not register")


# ---------------- Swarm mode inside Prometheus ----------------
async def test_a_request_can_be_sent_to_the_computers_you_picked_several_at_once():
    hub, base = await api_hub()
    try:
        mac, mini = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini")
        async with aiohttp.ClientSession(headers=H) as s:
            both = await (await s.post(base + "/api/jobs", json={"text": "update", "computers": ["MacBook", "mini"]})).json()
            assert sorted(x["node"] for x in both["subs"]) == ["Mac mini", "MacBook"]                   # a list: the same task to each
            one = await (await s.post(base + "/api/jobs", json={"text": "x", "computers": ["MacBook", "macbook"]})).json()
            assert [x["node"] for x in one["subs"]] == ["MacBook"]                                      # no duplicates
            gone = await (await s.post(base + "/api/jobs", json={"text": "x", "computers": ["MacBook", "Cray-1"]})).json()
            assert gone["state"] == "failed" and "Cray-1 is not online" in gone["result"]               # a picked computer that went offline: clear failure
    finally:
        await hub.stop()


def _swarm_mode(tmp_path, monkeypatch, base):
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory
    from voicectl.modes.base import ModeContext
    from voicectl.modes.prometheus import PrometheusMode
    from voicectl.agents import runner as rm
    monkeypatch.setenv("SWARM_TOKEN", "s3cret")
    spoken = []

    class Spk:
        active = False
        first_audio_at = 0.0
        async def speak_many(self, chunks, clean=True, show=False):
            async for c in chunks: spoken.append(c)
        async def speak(self, text): spoken.append(text)
        def stop(self): pass
        def wake(self): pass
    ctx = ModeContext(Config({"swarm": {"enabled": True, "api_url": base}, "tts": {"enabled": True}, "agent": {"fast_chat": False},
                              "browser": {"backend": "playwright"}}), EventBus())
    ctx.speaker = Spk()
    m = PrometheusMode(ctx)
    m._runner = lambda: rm.AgentRunner(ctx.config, ctx.bus, Memory(tmp_path / "m.db"))
    return m, ctx, spoken


async def test_swarm_mode_sends_the_request_to_the_computers_and_says_one_final_answer(tmp_path, monkeypatch):
    hub, base = await api_hub()
    try:
        mac, mini = await node_peer(hub, "MacBook"), await node_peer(hub, "Mac mini")
        m, ctx, spoken = _swarm_mode(tmp_path, monkeypatch, base)
        assert m.swarm_on() is False                                                              # single until you switch
        m.set_swarm_mode(True)
        m.set_swarm_targets(["MacBook"])                                                           # picked by hand
        turn = asyncio.create_task(m.respond("check for software updates"))
        t = await mac.get("task")
        assert t["text"] == "check for software updates" and not mini.ws.close_code                # only the MacBook was asked
        await mac.send(type="done", sub=t["sub"], ok=True, result="Everything is up to date.")
        out = await asyncio.wait_for(turn, 15)
        assert out == "Everything is up to date." and spoken == ["Everything is up to date."]      # one answer, spoken once
        m.set_swarm_targets([])                                                                    # auto: the swarm decides
        turn = asyncio.create_task(m.respond("run the backup on both"))
        t1, t2 = await mac.get("task"), await mini.get("task")
        await mac.send(type="done", sub=t1["sub"], ok=True, result="a"); await mini.send(type="done", sub=t2["sub"], ok=True, result="b")
        spoken.clear()
        await asyncio.wait_for(turn, 15)
        assert len(spoken) == 1                                                                    # two computers, still one spoken answer
    finally:
        await hub.stop()


async def test_swarm_mode_falls_back_to_this_computer_when_the_hub_is_unreachable_and_cancels_when_interrupted(tmp_path, monkeypatch):
    m, ctx, spoken = _swarm_mode(tmp_path, monkeypatch, "http://127.0.0.1:9")
    m.set_swarm_mode(True)
    assert await m._swarm_turn("anything") is None and spoken == []                                # nothing said: the request is handled here
    hub, base = await api_hub()
    try:
        mini = await node_peer(hub, "Mac mini")
        m, ctx, spoken = _swarm_mode(tmp_path, monkeypatch, base)
        m.set_swarm_mode(True)
        turn = asyncio.create_task(m._swarm_turn("long job"))
        t = await mini.get("task")
        await asyncio.sleep(0.4)                                                                   # the request is under way
        turn.cancel()                                                                              # you talked over it / pressed Esc
        await asyncio.gather(turn, return_exceptions=True)
        assert (await mini.get("cancel"))["sub"] == t["sub"]                                       # the work stops on the other computer too
    finally:
        await hub.stop()


def test_voice_commands_switch_between_swarm_and_single():
    from voicectl.modes.prometheus import SWARM_OFF, SWARM_ON
    for t in ["swarm mode", "use the swarm", "switch to swarm mode", "use all my computers", "turn on swarm mode"]:
        assert SWARM_ON.match(t), t
    for t in ["single mode", "just this computer", "switch to single", "swarm mode off", "only this mac"]:
        assert SWARM_OFF.match(t), t
    assert not SWARM_ON.match("the swarm of bees") and not SWARM_OFF.match("open single player mode")


async def test_a_thank_you_while_the_swarm_works_cancels_nothing_but_stop_and_goodbye_behave(tmp_path, monkeypatch):
    hub, base = await api_hub()
    try:
        mini = await node_peer(hub, "Mac mini")
        m, ctx, spoken = _swarm_mode(tmp_path, monkeypatch, base)
        m.set_swarm_mode(True)
        assert m.keep_turn_on_speech() is False                                                     # nothing running: speech is just speech
        turn = asyncio.create_task(m._swarm_turn("open linkedin"))
        t = await mini.get("task")
        await asyncio.sleep(0.4)
        assert m._swarm_active and m.keep_turn_on_speech() is True                                  # hearing you does not cancel the work
        assert await m.intercept("Thank you.") is True and not turn.done()                          # a thank-you is an acknowledgement
        assert hub.nodes["Mac mini"].status == "busy"
        turn.cancel()                                                                              # you said "stop" (or Esc)
        await asyncio.gather(turn, return_exceptions=True)
        assert (await mini.get("cancel"))["sub"] == t["sub"]
        # saying goodbye leaves the work running (detach), instead of killing it
        m2, ctx2, spoken2 = _swarm_mode(tmp_path, monkeypatch, base)
        m2.set_swarm_mode(True)
        await mini.send(type="done", sub=t["sub"], ok=False, result="stopped")
        await asyncio.sleep(0.2)
        turn2 = asyncio.create_task(m2._swarm_turn("open gmail"))
        t2 = await mini.get("task")
        await asyncio.sleep(0.4)
        m2._swarm_detach = m2._swarm_active                                                         # what _goodbye does
        turn2.cancel()
        await asyncio.gather(turn2, return_exceptions=True)
        await asyncio.sleep(0.3)
        assert hub.nodes["Mac mini"].status == "busy" and hub.subs[t2["sub"]].state == "running"    # still going on the other computer
        await mini.send(type="done", sub=t2["sub"], ok=True, result="Gmail is open")
    finally:
        await hub.stop()
