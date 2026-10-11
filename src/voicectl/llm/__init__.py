"""The language model, behind one interface. The rest of the code asks for text, a tool choice or a stream and never
touches an SDK, a client object or a response shape. Swap the provider (or use a fake in tests) with `set_llm(...)`."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


class LLM(Protocol):
    def text(self, *, model: str, prompt: str, max_tokens: int, system: str | None = None) -> str:
        """One short completion. Blocking: call it with asyncio.to_thread from async code."""

    def tool_calls(self, *, model: str, messages: list[dict], tools: list[dict], tool_choice: dict,
                   max_tokens: int, system: str | None = None) -> list[ToolCall]:
        """The tools the model chose to call (a forced choice returns that one tool's arguments). Blocking."""

    def stream(self, *, model: str, messages: list[dict], max_tokens: int, system: str | None = None) -> AsyncIterator[str]:
        """The reply as it is written, piece by piece."""


_current: LLM | None = None


def current() -> LLM:
    global _current
    if _current is None:
        from voicectl.llm.anthropic import AnthropicLLM
        _current = AnthropicLLM()
    return _current


def set_llm(llm: LLM | None) -> None:
    """Swap the implementation (tests, another provider). None restores the default."""
    global _current
    _current = llm
