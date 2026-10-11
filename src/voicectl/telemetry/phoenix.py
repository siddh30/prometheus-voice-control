from __future__ import annotations

import logging

from opentelemetry import trace

from voicectl.core.config import Config

log = logging.getLogger("voicectl.telemetry")
tracer = trace.get_tracer("voicectl")


def setup(config: Config) -> bool:
    """Send traces to local Phoenix. Safe to call when Phoenix isn't running (export just fails quietly)."""
    if not config.get("telemetry.enabled", True):
        return False
    try:
        from phoenix.otel import register
        provider = register(project_name=config.get("telemetry.project", "voicectl"),
                            endpoint=config.get("telemetry.endpoint", "http://localhost:6006/v1/traces"),
                            batch=True, auto_instrument=False, set_global_tracer_provider=True)
    except Exception as e:
        log.warning("Telemetry disabled: %s", e)
        return False
    try:  # agent-level spans (LLM calls, tool calls, cost) from the Claude Agent SDK
        from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor
        ClaudeAgentSDKInstrumentor().instrument(tracer_provider=provider)
    except Exception as e:
        log.warning("Claude Agent SDK instrumentation unavailable: %s", e)
    global tracer
    tracer = trace.get_tracer("voicectl")
    return True


def new_session_id() -> str:
    """A session id that IS its start time: epoch seconds, e.g. 1760198400 = 2026-10-11 12:00:00 UTC. All traces of one conversation share it."""
    import time
    return str(int(time.time()))


def session_label(session_id: str) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(session_id)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ""


def session_context(session_id: str):
    """Everything traced inside this `with` (including the Claude Agent SDK's own spans) is tagged `session.id`, so Phoenix groups
    the whole conversation under one session. A no-op if the helper isn't installed: tracing must never break a turn."""
    import contextlib
    try:
        from openinference.instrumentation import using_session
        return using_session(session_id)
    except Exception:
        return contextlib.nullcontext()
