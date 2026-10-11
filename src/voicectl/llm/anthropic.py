from __future__ import annotations

import os
from collections.abc import AsyncIterator

from voicectl.llm import ToolCall


class AnthropicLLM:
    """Claude through the Anthropic SDK. One client of each kind, reused: the connection is kept warm (saves ~0.5 s per call)."""

    def __init__(self) -> None:
        self._sync = None
        self._async = None

    def _client(self):
        if self._sync is None:
            from anthropic import Anthropic
            self._sync = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        return self._sync

    def _aclient(self):
        if self._async is None:
            from anthropic import AsyncAnthropic
            self._async = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        return self._async

    def text(self, *, model: str, prompt: str, max_tokens: int, system: str | None = None) -> str:
        extra = {"system": system} if system else {}
        r = self._client().messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}], **extra)
        return "".join(b.text for b in r.content if b.type == "text").strip()

    def tool_calls(self, *, model: str, messages: list[dict], tools: list[dict], tool_choice: dict,
                   max_tokens: int, system: str | None = None) -> list[ToolCall]:
        extra = {"system": system} if system else {}
        r = self._client().messages.create(model=model, max_tokens=max_tokens, tools=tools, tool_choice=tool_choice,
                                           messages=messages, **extra)
        return [ToolCall(b.name, dict(b.input)) for b in r.content if b.type == "tool_use"]

    async def stream(self, *, model: str, messages: list[dict], max_tokens: int, system: str | None = None) -> AsyncIterator[str]:
        extra = {"system": system} if system else {}
        async with self._aclient().messages.stream(model=model, max_tokens=max_tokens, messages=messages, **extra) as s:
            async for piece in s.text_stream:
                yield piece
