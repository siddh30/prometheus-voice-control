from __future__ import annotations

"""Where does the time go in a turn? One timeline per request: when you stopped talking, when the text arrived, when routing was
done, when the agent's first word came, every tool call (start and end), when the first sound played.

It is printed as one log line and also sent to Phoenix as a trace (http://localhost:6006, project `voicectl`), where the spans
show up as a waterfall: that is the graph for finding what is slow."""

import logging
import time

log = logging.getLogger("voicectl.timeline")


class TurnTimeline:
    def __init__(self, speech_end: float = 0.0, text_at: float = 0.0, clock=time.monotonic, session_id: str = "") -> None:
        self.clock = clock
        self.session_id = session_id
        self.marks: dict[str, float] = {}
        if speech_end:
            self.marks["speech_end"] = speech_end
        if text_at:
            self.marks["text"] = text_at
        self.tools: list[dict] = []
        self.kind = "agent"                       # which path answered: agent | chat | instant

    def mark(self, name: str, once: bool = True) -> None:
        if not once or name not in self.marks:
            self.marks[name] = self.clock()

    def tool_start(self, tool_id: str, name: str) -> None:
        self.tools.append({"id": tool_id, "name": name, "start": self.clock(), "end": None})

    def tool_end(self, tool_id: str, ok: bool = True) -> None:
        for t in self.tools:
            if t["id"] == tool_id and t["end"] is None:
                t["end"], t["ok"] = self.clock(), ok
                return

    # ---- reporting ----
    def summary(self, first_audio: float = 0.0, done: float = 0.0) -> str:
        m = self.marks
        parts, prev = [], m.get("speech_end")

        def seg(label: str, key: str) -> None:
            nonlocal prev
            if prev and key in m and m[key] >= prev:
                parts.append(f"{label} {m[key] - prev:.1f}s")
                prev = m[key]
        seg("speech-to-text", "text")
        seg("routing", "routed")
        seg("agent start", "query_sent")
        seg("model first token", "first_token")
        if self.tools:
            busy = sum((t["end"] or done or t["start"]) - t["start"] for t in self.tools)
            parts.append(f"{len(self.tools)} tool call(s) {busy:.1f}s")
        if first_audio and m.get("speech_end") and first_audio >= m["speech_end"]:
            parts.append(f"first sound at {first_audio - m['speech_end']:.1f}s")
        if done and m.get("speech_end"):
            parts.append(f"done at {done - m['speech_end']:.1f}s")
        sid = f"session {self.session_id} " if self.session_id else ""
        return f"{sid}[{self.kind}] " + " | ".join(parts) if parts else ""

    def emit(self, first_audio: float = 0.0) -> None:
        done = self.clock()
        line = self.summary(first_audio, done)
        if line:
            log.info("timeline: %s", line)
        try:
            self._trace(first_audio, done)
        except Exception as e:                    # telemetry must never break a turn
            log.debug("timeline trace skipped: %s", e)

    def _trace(self, first_audio: float, done: float) -> None:
        from opentelemetry import trace
        from voicectl.telemetry import phoenix as tel
        m = self.marks
        start = m.get("speech_end") or m.get("text")
        if not start:
            return
        off = time.time() - self.clock()          # monotonic -> wall clock
        ns = lambda t: int((t + off) * 1e9)       # noqa: E731
        attrs = {"session.id": self.session_id, "voicectl.session_started": tel.session_label(self.session_id)} if self.session_id else {}
        root = tel.tracer.start_span(f"turn ({self.kind})", start_time=ns(start), attributes=attrs)
        ctx = trace.set_span_in_context(root)

        def child(name: str, a: float | None, b: float | None, **attrs) -> None:
            if a and b and b >= a:
                s = tel.tracer.start_span(name, context=ctx, start_time=ns(a), attributes=attrs)
                s.end(end_time=ns(b))
        order = [("speech to text", "speech_end", "text"), ("routing", "text", "routed"), ("agent start", "routed", "query_sent"),
                 ("model: first token", "query_sent", "first_token")]
        for name, a, b in order:
            child(name, m.get(a), m.get(b))
        for t in self.tools:
            child(f"tool: {t['name']}", t["start"], t["end"] or done, ok=bool(t.get("ok", True)))
        if first_audio:
            child("first sound", m.get("text"), first_audio)
        root.end(end_time=ns(done))
