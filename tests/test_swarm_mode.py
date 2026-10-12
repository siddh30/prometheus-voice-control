"""F10 in the swarm app, end to end: the real session manager and conversation loop, the real router, controller and workers
(in process); only the microphone, speech-to-text and what each computer's agent does are stubs."""
import asyncio

import numpy as np

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.session import SessionManager
from voicectl.modes.base import ModeContext
from voicectl.modes.swarm import SwarmMode
from voicectl.swarm.controller import SwarmController
from voicectl.swarm.link import LocalLink
from voicectl.swarm.router import PC, Router
from voicectl.swarm.worker import Worker


class Mic:
    sample_rate = 16000
    on_level = None
    def start(self): ...
    def stop(self): return np.full(16000, 0.2, "float32")


class STT:
    def __init__(self, *words): self.words = list(words)
    def transcribe(self, audio, sr): return self.words.pop(0) if self.words else ""


class Exec:
    def __init__(self, name, log, approve=False): self.name, self.log, self.approve = name, log, approve
    async def run(self, task):
        self.log.append((self.name, task.text))
        if self.approve and not await task.ask_approval("Send this email?"):
            return "not sent"
        task.emit("checklist", items=[{"text": task.text, "status": "in_progress"}])
        return f"{self.name} finished {task.text}"
    async def cancel(self, task_id): ...


def build(*words, approve_on=None):
    cfg = Config({"tts": {"enabled": False}, "recording": {"min_hold_ms": 0, "mode": "toggle"}})
    bus, events = EventBus(), []
    real = bus.publish
    bus.publish = lambda t, **d: (events.append((t, d)) if t in ("swarm", "approval", "state") else None, real(t, **d))[1]
    log = []
    pcs = [PC("Studio", local=True), PC("MacBook", ("laptop",))]
    links = {p.name: LocalLink(p.name, Worker(p.name, Exec(p.name, log, approve=(p.name == approve_on)))) for p in pcs}
    controller = SwarmController(links, Router(pcs), bus, publish_every_s=0.02)
    ctx = ModeContext(cfg, bus)
    ctx.shared["swarm_controller"] = controller
    mode = SwarmMode(ctx)
    session = SessionManager(cfg, bus, Mic(), STT(*words), {"swarm": mode})
    return session, mode, controller, events, log


async def settle(t=0.3): await asyncio.sleep(t)


async def test_press_talk_press_and_the_request_lands_on_the_named_computer_silently():
    session, mode, controller, events, log = build("open gmail on the laptop")
    await controller.start()
    session.key_down("swarm"); await settle(0.1)               # F10: opens the conversation and starts recording
    session.key_down("swarm"); await settle(0.6)               # F10: send
    assert log == [("MacBook", "open gmail")]                  # routed by name; the routing words are not part of the task
    last = [d for t, d in events if t == "swarm"][-1]
    macbook = next(p for p in last["pcs"] if p["name"] == "MacBook")
    assert macbook["state"] == "done" and macbook["result"] == "MacBook finished open gmail"
    assert next(p for p in last["pcs"] if p["name"] == "Studio")["state"] in ("online", "idle")     # this computer was not involved
    assert last["routing"] == "named"
    session.cancel(); await settle(0.1); await controller.stop()


async def test_a_request_with_no_hint_runs_here_and_pressing_f10_mid_task_replaces_it():
    session, mode, controller, events, log = build("open linkedin", "open youtube")
    await controller.start()
    session.key_down("swarm"); await settle(0.1)
    session.key_down("swarm"); await settle(0.6)
    assert log == [("Studio", "open linkedin")]
    session.key_down("swarm"); await settle(0.1)               # a second recording
    session.key_down("swarm"); await settle(0.6)
    assert log[-1] == ("Studio", "open youtube")
    session.cancel(); await settle(0.1); await controller.stop()


async def test_a_computer_that_needs_approval_asks_and_a_typed_yes_answers_it():
    session, mode, controller, events, log = build("email bob on the macbook", approve_on="MacBook")
    await controller.start()
    session.key_down("swarm"); await settle(0.1)
    session.key_down("swarm"); await settle(0.5)
    assert controller.pending_approval == "MacBook: Send this email?"
    assert ("approval", {"text": "MacBook: Send this email?"}) in events          # the widget's usual Approve / Deny panel
    await mode.submit_text("yes"); await settle(0.5)                              # typed: an approval answer, not a new request
    last = [d for t, d in events if t == "swarm"][-1]
    assert next(p for p in last["pcs"] if p["name"] == "MacBook")["state"] == "done"
    session.cancel(); await settle(0.1); await controller.stop()


async def test_without_a_controller_the_mode_says_how_to_set_it_up():
    cfg = Config({"tts": {"enabled": False}})
    bus, errors = EventBus(), []
    real = bus.publish
    bus.publish = lambda t, **d: (errors.append(d.get("message")) if t == "error" else None, real(t, **d))[1]
    mode = SwarmMode(ModeContext(cfg, bus))
    try:
        await mode.respond("hello")
    except RuntimeError as e:
        assert "config.local.yaml" in str(e)
