"""Routes: a task that hit failures and still got there is remembered whole (goal, what failed, what worked instead) and shown to the
next similar task, in the same conversation or a later one."""
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from voicectl.agents import runner as rm
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.learnings import LearningStore
from voicectl.core.memory import Memory

GOAL = "show me recent posts about product management"


def store(tmp_path):
    return LearningStore(tmp_path / "learnings.db")


def test_a_route_is_saved_with_its_goal_and_found_again_for_a_similarly_worded_task(tmp_path):
    s = store(tmp_path)
    assert s.record_route("www.linkedin.com", GOAL, [("browser_click", "posts filter")], [("browser_goto", "/search/results")]) is True
    found = s.relevant_routes("find me posts on product management", "linkedin.com")
    assert len(found) == 1 and "posts filter" in found[0] and "/search/results" in found[0] and "failed" in found[0]
    assert s.relevant_routes("find product management posts", "") != []            # no page open yet: the goal alone is enough


def test_unrelated_tasks_and_other_sites_do_not_get_the_route(tmp_path):
    s = store(tmp_path)
    s.record_route("linkedin.com", GOAL, [("browser_click", "posts filter")], [("browser_goto", "/search/results")])
    assert s.relevant_routes("play a song on youtube", "youtube.com") == []
    assert s.relevant_routes("show me my recent orders", "amazon.com") == []        # one shared word is not a similar task


def test_the_same_task_again_strengthens_the_route_and_keeps_the_shortest_way(tmp_path):
    s = store(tmp_path)
    s.record_route("linkedin.com", GOAL, [("browser_click", "posts filter")], [("browser_click", "search box"), ("browser_goto", "/search/results")])
    assert s.record_route("linkedin.com", "show me the recent posts about product management", [("browser_click", "sort menu")],
                          [("browser_goto", "/search/results")]) is False           # same task: not a second route
    assert len(s.routes()) == 1
    text = s.relevant_routes(GOAL, "linkedin.com")[0]
    assert "sort menu" in text and "posts filter" in text                           # everything that failed is remembered...
    assert "search box" not in text                                                 # ...and the shortest route that worked is kept


def test_nothing_is_saved_without_a_failure_a_site_or_a_real_goal(tmp_path):
    s = store(tmp_path)
    assert s.record_route("linkedin.com", GOAL, [], [("browser_goto", "/x")]) is False          # nothing failed
    assert s.record_route("", GOAL, [("browser_click", "a")], [("browser_goto", "/x")]) is False    # no site
    assert s.record_route("linkedin.com", "do it", [("browser_click", "a")], [("browser_goto", "/x")]) is False   # too vague to match later
    assert s.routes() == []


def test_routes_can_be_forgotten_pruned_and_cleared_like_lessons(tmp_path):
    s = store(tmp_path)
    for i, goal in enumerate(["find recent posts about product management", "download quarterly invoice statement pdf", "update billing address profile page"]):
        s.record_route(f"site{i}.com", goal, [("browser_click", "a")], [("browser_goto", "/b")])
    assert s.forget_learning("invoice") == 1 and len(s.routes()) == 2
    assert s.prune_routes(1) == 1 and len(s.routes()) == 1
    s.clear()
    assert s.routes() == []


# ---- the whole thing through the agent, over a conversation ----
class Scripted:
    """An agent whose first request: a click that fails, then a direct page that works."""
    def __init__(self, fail=True, then_ok=True):
        self.fail, self.then_ok, self.prompts = fail, then_ok, []
    async def query(self, p): self.prompts.append(p)
    async def interrupt(self): pass
    def receive_response(self):
        async def gen():
            yield AssistantMessage(content=[ToolUseBlock(id="a", name="mcp__browser__browser_click", input={"index": 4, "description": "Posts filter"})], model="m")
            yield UserMessage(content=[ToolResultBlock(tool_use_id="a", content="boom" if self.fail else "ok", is_error=self.fail)])
            if self.then_ok:
                yield AssistantMessage(content=[ToolUseBlock(id="b", name="mcp__browser__browser_goto", input={"url": "https://www.linkedin.com/search/results/content/?keywords=x"})], model="m")
                yield UserMessage(content=[ToolResultBlock(tool_use_id="b", content="ok", is_error=False)])
        return gen()


def make_runner(tmp_path, client):
    mem = Memory(tmp_path / "m.db")
    r = rm.AgentRunner(Config({"browser": {"backend": "playwright"}}), EventBus(), mem)
    r.browser.last_url = "https://www.linkedin.com/feed/"
    async def ensure(): return client, mem.new_session()
    r._ensure_session = ensure
    return r


async def run(r, text):
    return [ev async for ev in r.stream_turn(text, complexity="multi")]


async def test_a_failure_then_a_working_route_is_saved_and_shown_to_the_next_similar_task_in_the_same_conversation(tmp_path):
    c = Scripted()
    r = make_runner(tmp_path, c)
    await run(r, GOAL)
    assert "<Routes>" not in c.prompts[0]                                           # the first time there was nothing to go on
    assert r.learnings.routes() and "posts filter" in r.learnings.relevant_routes("recent posts product management", "linkedin.com")[0]
    await run(r, "now find me posts about product management again")                # same conversation, new wording
    second = c.prompts[1]
    assert "<Routes>" in second and "posts filter" in second and "/search/results" in second
    assert "<EarlierInThisConversation>" in second and "posts filter" in second.lower()    # the dead end is carried forward too


async def test_a_later_conversation_gets_the_route_too_and_a_new_conversation_forgets_the_dead_ends(tmp_path):
    first = make_runner(tmp_path, Scripted())
    await run(first, GOAL)
    await first.end_session()
    assert first._dead_ends == []                                                   # a new conversation starts without the old dead ends
    c2 = Scripted(fail=False, then_ok=False)
    second = make_runner(tmp_path, c2)                                              # a new runner on the same data folder (an app restart)
    await run(second, "pull up product management posts from this week")
    assert "<Routes>" in c2.prompts[0] and "/search/results" in c2.prompts[0]       # the route survives restarts
    assert "<EarlierInThisConversation>" not in c2.prompts[0]


async def test_nothing_is_saved_when_nothing_failed_or_the_task_never_got_there(tmp_path):
    ok = make_runner(tmp_path, Scripted(fail=False, then_ok=True))
    await run(ok, GOAL)
    assert ok.learnings.routes() == []                                              # no failure, nothing to learn
    (tmp_path / "stuck").mkdir()
    stuck = make_runner(tmp_path / "stuck", Scripted(fail=True, then_ok=False))
    await run(stuck, GOAL)
    assert stuck.learnings.routes() == []                                           # it failed and never recovered: no route...
    assert stuck._dead_ends                                                         # ...but the failure is remembered for the next turns
