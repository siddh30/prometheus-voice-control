"""Thumbs up / thumbs down on a run, with an optional note, for every mode (single, swarm, ...).

It listens to the same events the widget gets, so it knows the last request, which mode took it, what the steps and the result were,
and (in the swarm) which computer did what. A rating is one line of JSON in `feedback.jsonl` next to the memory: easy to read,
easy to grep, easy to hand back ("these runs were good"). Nothing here touches the agent: it only remembers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from voicectl.core.events import EventBus

log = logging.getLogger("voicectl.feedback")

RATINGS = ("up", "down")
MAX_TEXT = 2000
MAX_NOTE = 1000


def _clip(text: Any, n: int = MAX_TEXT) -> str:
    return str(text or "")[:n]


class FeedbackLog:
    def __init__(self, bus: EventBus, path: Path, clock=time.time, learn: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.bus, self.path, self._clock = bus, Path(path), clock
        self.learn = learn                                        # told about every saved rating, so the next run can use it
        self.run: dict[str, Any] = {}
        self._task: asyncio.Task | None = None

    # ---- following the run ----
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._follow())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _follow(self) -> None:
        async for ev in self.bus.subscribe():
            try:
                self.observe(ev.type, ev.data)
            except Exception:
                log.debug("could not follow %s", ev.type, exc_info=True)

    def observe(self, kind: str, data: dict[str, Any]) -> None:
        """One event from the daemon. A new transcript starts a new run; everything after it belongs to that run."""
        if kind == "transcript":
            self.run = {"request": _clip(data.get("text")), "mode": data.get("mode", ""), "steps": [], "started": self._clock()}
        elif not self.run:
            return
        elif kind == "route":
            self.run["route"] = {k: data[k] for k in ("kind", "how", "action") if k in data}
        elif kind == "step" and data.get("text"):
            self.run["steps"] = (self.run["steps"] + [{"text": _clip(data["text"], 200), "failed": bool(data.get("failed"))}])[-40:]
        elif kind == "checklist":
            self.run["checklist"] = [{"text": _clip(i.get("text"), 200), "status": i.get("status", "")} for i in data.get("items", [])][:20]
        elif kind == "result":
            self.run["result"] = _clip(data.get("text"))
        elif kind == "error":
            self.run["error"] = _clip(data.get("message"), 400)
        elif kind == "swarm":
            self.run["swarm"] = {"routing": data.get("routing", ""), "pcs": [
                {k: _clip(pc.get(k), 600) for k in ("name", "state", "task", "result", "error")} for pc in data.get("pcs", [])
                if pc.get("task") or pc.get("result") or pc.get("error")]}

    # ---- the rating ----
    def record(self, rating: str, note: str = "") -> bool:
        """Save the rating against the latest run. False when there is nothing to rate or the rating is not understood."""
        if rating not in RATINGS or not self.run.get("request"):
            return False
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock())), "rating": rating, "note": _clip(note, MAX_NOTE).strip(),
                 **{k: v for k, v in self.run.items() if k != "started"}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("could not save feedback: %s", e)
            return False
        if self.learn:
            try:
                self.learn(entry)
            except Exception:
                log.warning("could not learn from feedback", exc_info=True)           # the rating itself is saved either way
        log.info("feedback: %s%s (%s)", rating, f" with a note" if entry["note"] else "", entry["mode"] or "?")
        return True

    @staticmethod
    def how(entry: dict[str, Any]) -> str:
        """The run in one line: which computers did what (swarm), else the steps it took."""
        if entry.get("swarm", {}).get("pcs"):
            return "; ".join(f"{p['name']}: {p.get('task') or p.get('result', '')}"[:120] for p in entry["swarm"]["pcs"])
        steps = [s["text"] for s in entry.get("steps", []) if not s.get("failed")][:6]
        return ", then ".join(steps)

    def entries(self, rating: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if rating is None or e.get("rating") == rating:
                out.append(e)
        return out
