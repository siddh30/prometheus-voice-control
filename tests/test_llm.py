import pathlib
import re

from voicectl import llm
from voicectl.llm import ToolCall


class FakeLLM:
    def __init__(self, text="", calls=()):
        self._text, self._calls, self.seen = text, list(calls), []

    def text(self, **kw):
        self.seen.append(("text", kw)); return self._text

    def tool_calls(self, **kw):
        self.seen.append(("tool_calls", kw)); return self._calls

    async def stream(self, **kw):
        self.seen.append(("stream", kw))
        for p in self._text.split("|"):
            yield p


async def test_memory_calls_go_through_the_llm_interface_not_an_sdk():
    from voicectl.core.maintenance import extract_memory, summarize
    fake = FakeLLM('Here you go: {"facts": [{"key": "role", "value": "PM"}], "learnings": []}')
    llm.set_llm(fake)
    assert (await extract_memory("log"))["facts"][0]["key"] == "role"
    fake = FakeLLM("A short digest.")
    llm.set_llm(fake)
    assert await summarize("log") == "A short digest."
    assert fake.seen[0][1]["model"] == "claude-haiku-4-5" and "log" in fake.seen[0][1]["prompt"]


async def test_the_router_reads_tool_choices_from_the_interface():
    from voicectl.modes.router import LLMDecider
    llm.set_llm(FakeLLM(calls=[ToolCall("agent_task", {})]))
    route = await LLMDecider("claude-haiku-4-5").decide("find me a job", [], None)
    assert route.kind == "agent" and route.how == "llm"
    llm.set_llm(FakeLLM(calls=[]))                                                 # no tool chosen: chat
    assert (await LLMDecider("claude-haiku-4-5").decide("hello", [], None)).kind == "chat"


async def test_streaming_comes_from_the_interface():
    fake = FakeLLM("Hel|lo")
    llm.set_llm(fake)
    out = [p async for p in llm.current().stream(model="m", messages=[], max_tokens=5)]
    assert out == ["Hel", "lo"] and fake.seen[0][1]["model"] == "m"


def test_the_anthropic_sdk_is_imported_only_inside_the_llm_package():
    offenders = []
    for p in pathlib.Path("src/voicectl").rglob("*.py"):
        if "llm" in p.parts:
            continue
        if re.search(r"^\s*(from|import) anthropic\b", p.read_text(), re.M):
            offenders.append(str(p))
    assert not offenders, f"ask the model through voicectl.llm instead: {offenders}"
