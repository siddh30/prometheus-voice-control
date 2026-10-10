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
