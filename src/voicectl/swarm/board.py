from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from voicectl.swarm.link import LinkState

# What the widget shows per computer.
IDLE, WORKING, WAITING, STALLED, DONE, FAILED = "idle", "working", "waiting", "stalled", "done", "failed"
CONNECTING, NOT_RESPONDING, OFFLINE = "connecting", "not_responding", "offline"


@dataclass
class PCState:
    name: str
    local: bool = False
    link: str = CONNECTING                       # connecting | online | not_responding | offline (the connection)
    detail: str = ""                             # why it is offline
    task_id: str = ""
    task: str = ""
    run: str = IDLE                              # idle | working | waiting | stalled | done | failed (the work)
    checklist: list[dict[str, str]] = field(default_factory=list)
    step: str = ""
    result: str = ""
    error: str = ""
    approval: str = ""

    @property
    def state(self) -> str:
        """The one word the widget shows: the connection if it is a problem, otherwise the work."""
        if self.link in (OFFLINE, NOT_RESPONDING, CONNECTING):
            return self.link
        return self.run

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "local": self.local, "state": self.state, "detail": self.detail, "task": self.task,
                "checklist": list(self.checklist), "step": self.step, "result": self.result, "error": self.error,
                "approval": self.approval}


class Board:
    """Everything the widget needs to draw the swarm, in one place, updated as workers report. It knows nothing about
    networks or agents: events go in, a `swarm` event payload comes out."""

    def __init__(self, pcs: list[tuple[str, bool]]) -> None:
        self.pcs: dict[str, PCState] = {name: PCState(name, local) for name, local in pcs}
        self.picked: str = "auto"
        self.routing: str = ""
        self.revision = 0

    def _touch(self) -> None:
        self.revision += 1

    # ---- connections ----
    def link_state(self, pc: str, state: LinkState, detail: str = "") -> bool:
        s = self.pcs.get(pc)
        if not s or (s.link, s.detail) == (state.value, detail):
            return False
        s.link, s.detail = state.value, detail
        if state is LinkState.OFFLINE and s.run in (WORKING, WAITING, STALLED):
            s.run, s.error = FAILED, "went offline while working"
        self._touch()
        return True

    # ---- work ----
    def start(self, pc: str, task_id: str, task: str) -> None:
        s = self.pcs[pc]
        s.task_id, s.task, s.run = task_id, task, WORKING
        s.checklist, s.step, s.result, s.error, s.approval = [], "", "", "", ""
        self._touch()

    def fail(self, pc: str, error: str) -> None:
        s = self.pcs[pc]
        s.run, s.error, s.approval = FAILED, error, ""
        self._touch()

    def apply(self, pc: str, event: dict[str, Any]) -> bool:
        """One worker event. Returns whether anything visible changed. Events for a task that is no longer current are ignored."""
        s = self.pcs.get(pc)
        if not s or (event.get("id") and event["id"] != s.task_id):
            return False
        kind = event.get("type")
        if kind == "checklist":
            s.checklist = [{"text": str(i.get("text", "")), "status": str(i.get("status", "pending"))} for i in event.get("items", [])]
        elif kind == "step":
            s.step = "" if event.get("done") else str(event.get("text", ""))
        elif kind == "result":
            s.result = str(event.get("text", ""))
        elif kind == "approval":
            s.run, s.approval = WAITING, str(event.get("text", ""))
        elif kind == "stalled":
            s.run = STALLED
        elif kind in ("resumed", "started"):
            s.run, s.approval = WORKING, ""
        elif kind == "done":
            s.run, s.step, s.approval = DONE, "", ""
            s.checklist = [{**i, "status": "done"} if i["status"] == "in_progress" else i for i in s.checklist]
        elif kind == "failed":
            s.run, s.step, s.approval, s.error = FAILED, "", "", str(event.get("error", "failed"))
        else:
            return False
        if kind != "approval" and s.run == WAITING:
            s.run = WORKING
        self._touch()
        return True

    def reset(self, pc: str) -> None:
        """Work on this computer was cancelled: its card goes back to resting."""
        s = self.pcs[pc]
        s.task_id = s.task = s.step = s.result = s.error = s.approval = ""
        s.run, s.checklist = IDLE, []
        self._touch()

    def pick(self, pc: str) -> None:
        self.picked = pc if pc in self.pcs else "auto"
        self._touch()

    def set_routing(self, how: str) -> None:
        self.routing = how
        self._touch()

    # ---- what the widget gets ----
    def snapshot(self) -> dict[str, Any]:
        return {"pcs": [s.view() for s in self.pcs.values()], "picked": self.picked, "routing": self.routing}

    def working(self) -> list[PCState]:
        return [s for s in self.pcs.values() if s.run in (WORKING, WAITING, STALLED)]

    def pending_approval(self) -> PCState | None:
        return next((s for s in self.pcs.values() if s.run == WAITING), None)
