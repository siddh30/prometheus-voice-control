"""The worker's agent is single mode's agent: same prompt, same instant path for exact commands, and it still opens a page when
browser automation isn't available."""
import pathlib
import types

from voicectl import platform
from voicectl.agents.runner import Finished
from voicectl.core.config import Config
from voicectl.core.memory import Memory
from voicectl.swarm.executor import AgentExecutor, PROMPT
from voicectl.swarm.worker import TaskContext


class Agent:
    """Stands in for the real agent and records whether it was needed."""
    def __init__(self): self.asked = []
    async def stream_turn(self, text, context="", hints="", complexity="", publish_result=True):
        self.asked.append(text)
        yield Finished("the agent answered")
    def respond_approval(self, ok): ...
    async def interrupt(self): ...


class OS:
    def __init__(self, code=0): self.calls, self.code = [], code
    def run(self, cmd, **k):
        self.calls.append(cmd)
        return types.SimpleNamespace(returncode=self.code, stdout="", stderr="no such app")


def make(tmp_path):
    executor = AgentExecutor(Config({"browser": {"backend": "playwright"}}), Memory(tmp_path / "m.db"))
    executor.runner = Agent()
    return executor


async def run(executor, text):
    steps = []
    ctx = TaskContext("t1", text, "", lambda kind, **f: steps.append((kind, f)), lambda q: None)
    return await executor.run(ctx), steps


def test_the_worker_uses_the_same_agent_prompt_as_single_mode(tmp_path):
    executor = AgentExecutor(Config({"browser": {"backend": "playwright"}}), Memory(tmp_path / "m.db"))
    assert PROMPT == "prometheus_prompt.txt" and executor.runner.prompt_name == "prometheus_prompt.txt"


async def test_an_exact_open_command_runs_instantly_with_no_agent_and_no_browser_automation(tmp_path):
    fake = OS()
    platform.set_platform(fake)
    executor = make(tmp_path)
    final, steps = await run(executor, "Can you open linkedin please")
    assert executor.runner.asked == []                                   # the agent was never involved
    assert fake.calls and fake.calls[0][0] == "open"                     # the operating system opened it
    assert final and steps and steps[-1][0] == "step" and steps[-1][1]["done"] is True


async def test_anything_else_goes_to_the_agent(tmp_path):
    platform.set_platform(OS())
    executor = make(tmp_path)
    final, _ = await run(executor, "Open the LinkedIn page in Chrome and read me the first three posts")
    assert final == "the agent answered" and executor.runner.asked


async def test_an_instant_command_that_fails_is_handed_to_the_agent_instead_of_failing(tmp_path):
    platform.set_platform(OS(code=1))
    executor = make(tmp_path)
    final, _ = await run(executor, "open linkedin")
    assert final == "the agent answered"


def test_the_prompt_says_to_still_open_the_page_when_browser_control_is_declined():
    text = pathlib.Path("prompts/prometheus_prompt.txt").read_text()
    section = text[text.index("## When something fails"):]
    assert "computer_action" in section and "open_url" in section and "declined" in section
    assert "Never ask for browser setup just to open something" in section


def test_the_instant_path_is_one_shared_function_for_single_mode_and_the_swarm():
    from voicectl import actions as pkg
    from voicectl.actions.base import actions
    from voicectl.actions.router import quick_action
    from voicectl.modes.router import fast_route
    actions.discover(pkg)
    cls, args = quick_action("can you open safari for me")
    route = fast_route("can you open safari for me")
    assert route.kind == "action" and route.action is cls and route.args == args
    assert quick_action("open the linkedin page in chrome and then read the first three posts") is None
