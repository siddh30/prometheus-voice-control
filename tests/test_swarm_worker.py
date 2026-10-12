import asyncio

from voicectl.swarm.protocol import EVENTS, validate
from voicectl.swarm.worker import Worker


class Scripted:
    """An executor whose behaviour the test controls."""

    def __init__(self, script):
        self.script, self.cancelled = script, []

    async def run(self, task):
        return await self.script(task)

    async def cancel(self, task_id):
        self.cancelled.append(task_id)


class Conn:
    def __init__(self): self.sent = []
    def send(self, message): self.sent.append(message)
    def kinds(self): return [m["type"] for m in self.sent]


def setup(script, **kw):
    now = [1000.0]
    worker = Worker("macbook", Scripted(script), clock=lambda: now[0], **kw)
    conn = Conn()
    return worker, worker.attach(conn.send), conn, now


async def settle(t=0.05): await asyncio.sleep(t)


async def test_progress_arrives_in_order_and_every_event_honours_the_protocol():
    async def script(task):
        task.emit("checklist", items=[{"text": "Open LinkedIn", "status": "in_progress"}])
        task.emit("step", text="Opened linkedin.com", done=True)
        task.emit("checklist", items=[{"text": "Open LinkedIn", "status": "done"}])
        return "Opened LinkedIn."
    worker, session, conn, _ = setup(script)
    await worker.command(session, {"type": "task", "id": "t1", "text": "open linkedin"})
    await settle()
    assert conn.kinds() == ["started", "checklist", "step", "checklist", "result", "done"]
    assert conn.sent[-2] == {"type": "result", "id": "t1", "text": "Opened LinkedIn."}
    assert all(validate(EVENTS, m) is None for m in conn.sent)


async def test_a_task_that_goes_quiet_is_reported_as_stalled_and_resumes_when_it_moves():
    gate = asyncio.Event()
    async def script(task):
        task.emit("step", text="Searching", done=False)
        await gate.wait()
        task.emit("step", text="Searched", done=True)
        return "ok"
    worker, session, conn, now = setup(script, stall_after_s=30)
    await worker.command(session, {"type": "task", "id": "t1", "text": "x"})
    await settle()
    await worker.check_stalls()
    assert "stalled" not in conn.kinds()                      # quiet for no time at all
    now[0] += 31
    await worker.check_stalls()
    await worker.check_stalls()                               # reported once, not on every tick
    assert conn.kinds().count("stalled") == 1 and conn.sent[-1]["seconds"] >= 30
    gate.set(); await settle()
    assert conn.kinds()[-4:] == ["resumed", "step", "result", "done"]


async def test_waiting_for_your_yes_or_no_is_not_a_stall_and_the_answer_reaches_the_task():
    async def script(task):
        ok = await task.ask_approval("Send this email?")
        return "sent" if ok else "not sent"
    worker, session, conn, now = setup(script, stall_after_s=10)
    await worker.command(session, {"type": "task", "id": "t1", "text": "email bob"})
    await settle()
    assert conn.sent[-1] == {"type": "approval", "id": "t1", "text": "Send this email?"}
    now[0] += 500
    await worker.check_stalls()
    assert "stalled" not in conn.kinds()
    await worker.command(session, {"type": "approval", "id": "t1", "approved": False})
    await settle()
    assert conn.sent[-2]["text"] == "not sent" and conn.kinds()[-1] == "done"


async def test_cancel_stops_the_executor_and_says_so():
    async def script(task):
        await asyncio.sleep(30)
    worker, session, conn, _ = setup(script)
    await worker.command(session, {"type": "task", "id": "t1", "text": "long"})
    await settle()
    await worker.command(session, {"type": "cancel", "id": "t1"})
    assert worker.executor.cancelled == ["t1"]
    assert conn.sent[-1] == {"type": "failed", "id": "t1", "error": "cancelled"}


async def test_an_executor_error_becomes_a_failed_event_and_the_worker_carries_on():
    async def script(task):
        if task.text == "bad":
            raise RuntimeError("Chrome is not reachable")
        return "fine"
    worker, session, conn, _ = setup(script)
    await worker.command(session, {"type": "task", "id": "a", "text": "bad"})
    await worker.command(session, {"type": "task", "id": "b", "text": "good"})
    await settle()
    assert {"type": "failed", "id": "a", "error": "RuntimeError: Chrome is not reachable"} in conn.sent
    assert {"type": "done", "id": "b"} in conn.sent


async def test_a_controller_that_disconnects_takes_its_tasks_with_it():
    async def script(task):
        await asyncio.sleep(30)
    worker, session, conn, _ = setup(script)
    await worker.command(session, {"type": "task", "id": "t1", "text": "long"})
    await settle()
    await worker.detach(session)
    assert worker.executor.cancelled == ["t1"] and not worker._running


async def test_bad_or_repeated_commands_are_ignored():
    runs = []
    async def script(task):
        runs.append(task.id); await asyncio.sleep(0.1); return "ok"
    worker, session, conn, _ = setup(script)
    await worker.command(session, {"type": "task", "text": "no id"})
    await worker.command(session, {"type": "launch_missiles"})
    await worker.command(session, {"type": "task", "id": "t1", "text": "x"})
    await worker.command(session, {"type": "task", "id": "t1", "text": "x"})       # a repeated request must not run twice
    await settle(0.3)
    assert runs == ["t1"]
