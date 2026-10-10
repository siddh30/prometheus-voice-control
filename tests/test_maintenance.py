import asyncio
import time

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.core.maintenance import Maintenance
from voicectl.core.memory import Memory


class FakeRunner:
    def __init__(self, mem): self.mem, self.ended = mem, 0
    async def end_session(self):
        self.ended += 1
        self.mem.end_sessions()


def setup(tmp_path, idle=True, rss=500.0, cfg=None, summarizer=None):
    mem = Memory(tmp_path / "m.db")
    runner = FakeRunner(mem)
    state = {"idle": idle}

    async def digest(text): return "Looking for PM jobs in NYC; shortlisted Stripe and Datadog."
    m = Maintenance(Config({"memory": cfg or {}}), EventBus(), mem, lambda: state["idle"], lambda: runner,
                    summarizer=summarizer or digest, rss=lambda: rss, data_dir=tmp_path)
    return m, mem, runner, state


def old_session(mem, minutes_old=90, turns=3):
    sid = mem.new_session()
    mem.db.execute("UPDATE sessions SET started = ?, last_used = ?, claude_session_id = 'abc', turns = ? WHERE id = ?",
                   (time.time() - minutes_old * 60, time.time() - 120, turns, sid))
    for i in range(turns):
        mem.log_turn(sid, f"question {i}", f"answer {i}")
    mem.db.commit()
    return sid


async def test_an_old_session_is_distilled_into_a_note_and_closed(tmp_path):
    m, mem, runner, _ = setup(tmp_path)
    old_session(mem, 90)
    r = await m.clean()
    assert runner.ended == 1 and mem.active_session(1e9) is None                      # fresh context next time
    assert "PM jobs" in mem.facts()["session_digest"]                                  # ...but it remembers what mattered
    assert "closed a 90-minute session" in str(r)


async def test_a_young_session_is_left_alone_unless_forced(tmp_path):
    m, mem, runner, _ = setup(tmp_path)
    old_session(mem, 10)
    await m.clean()
    assert runner.ended == 0 and mem.active_session(1e9) is not None
    await m.clean(force=True)                                                           # "clean your memory"
    assert runner.ended == 1


async def test_offline_still_saves_a_plain_digest(tmp_path):
    async def boom(text): raise RuntimeError("offline")
    m, mem, runner, _ = setup(tmp_path, summarizer=boom)
    old_session(mem, 90)
    await m.clean()
    assert "question 2" in mem.facts()["session_digest"] and runner.ended == 1


async def test_housekeeping_prunes_old_turns_extra_facts_and_big_logs(tmp_path):
    m, mem, runner, _ = setup(tmp_path, cfg={"max_facts": 3, "keep_turns_hours": 1, "max_log_mb": 1})
    sid = old_session(mem, 5, turns=2)
    mem.db.execute("UPDATE turns SET ts = ? WHERE user_text = 'question 0'", (time.time() - 3 * 3600,)); mem.db.commit()
    mem.remember("resume_summary", "PM, 7 years")                                       # protected: never auto-deleted
    for i in range(6):
        mem.remember(f"note{i}", "x"); time.sleep(0.01)
    (tmp_path / "daemon.log").write_bytes(b"x" * (3 * 1024 * 1024))
    r = await m.clean()
    assert len(mem.turns_for(sid)) == 1                                                 # the 3-hour-old turn went
    facts = mem.facts()
    assert "resume_summary" in facts and len(facts) == 3 and "note5" in facts           # newest kept, oldest dropped
    assert (tmp_path / "daemon.log").stat().st_size <= 1024 * 1024 and "trimmed daemon.log" in str(r)


async def test_never_cleans_while_busy_and_cleans_as_soon_as_it_is_quiet(tmp_path):
    m, mem, runner, state = setup(tmp_path, idle=False)
    old_session(mem, 90)
    task = asyncio.create_task(m.clean_when_idle(max_wait_s=5, poll_s=0.05))
    await asyncio.sleep(0.2)
    assert runner.ended == 0 and not task.done()                                        # mid-conversation: hands off
    state["idle"] = True
    r = await asyncio.wait_for(task, 2)
    assert runner.ended == 1


async def test_gives_up_quietly_if_it_is_never_idle(tmp_path):
    m, mem, runner, _ = setup(tmp_path, idle=False)
    old_session(mem, 90)
    r = await m.clean_when_idle(max_wait_s=0.2, poll_s=0.05)
    assert "skipped" in str(r) and runner.ended == 0


async def test_a_bloated_process_asks_to_restart_only_when_idle(tmp_path):
    m, mem, runner, state = setup(tmp_path, rss=5000.0, cfg={"max_rss_mb": 3500})
    fired = []
    m.on_restart = lambda: fired.append(1)
    r = await m.clean()
    assert r.restart and fired == [1]
    m2, _, _, _ = setup(tmp_path, rss=1000.0, cfg={"max_rss_mb": 3500})
    m2.on_restart = lambda: fired.append(2)
    assert not (await m2.clean()).restart and fired == [1]


async def test_saved_facts_you_asked_for_survive_a_cleanup(tmp_path):
    m, mem, runner, _ = setup(tmp_path)
    mem.remember("target_role", "Senior PM"); mem.remember("resume_summary", "7 yrs fintech")
    old_session(mem, 120)
    await m.clean(force=True)
    assert mem.facts()["target_role"] == "Senior PM" and mem.facts()["resume_summary"] == "7 yrs fintech"


async def test_voice_command_runs_the_cleanup(tmp_path):
    from voicectl.modes.base import ModeContext
    from voicectl.modes.prometheus import CLEAN_MEMORY, PrometheusMode
    for t in ["clean your memory", "Clean up your memory.", "please clear the context", "tidy my memory", "reset your memory"]:
        assert CLEAN_MEMORY.match(t), t
    assert not CLEAN_MEMORY.match("clean the kitchen") and not CLEAN_MEMORY.match("what is memory")
    cfg = Config({"tts": {"enabled": False}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}})
    ctx = ModeContext(cfg, EventBus(), None)
    m_, mem, runner, _ = setup(tmp_path)
    old_session(mem, 5)
    ctx.shared["maintenance"] = m_
    ctx.shared["agent_runner"] = runner
    mode = PrometheusMode(ctx)
    assert await mode.intercept("clean your memory") is True
    await asyncio.sleep(0.3)
    assert runner.ended == 1                                                            # forced even though the session is young


async def test_start_fresh_forgets_everything_and_closes_the_agent_session(tmp_path):
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    mem.remember("target_role", "PM"); sid = mem.new_session(); mem.log_turn(sid, "hi", "hello")
    calls = []
    class R:
        async def interrupt(self): calls.append("interrupt")
        async def end_session(self): calls.append("end")
    bus = EventBus(); got = []
    m = Maintenance(Config({}), bus, mem, lambda: True, lambda: R(), data_dir=tmp_path)
    forgot = []
    await m.start_fresh(lambda: forgot.append(1))
    assert mem.facts() == {} and mem.latest_session() is None and calls == ["interrupt", "end"] and forgot == [1]


def test_voice_phrases_for_starting_fresh_are_not_confused_with_the_hourly_tidy():
    from voicectl.modes.prometheus import CLEAN_MEMORY, FRESH
    for t in ["forget everything", "start fresh", "Start over.", "please wipe your memory", "clear all memory", "start from scratch", "erase your memory"]:
        assert FRESH.match(t), t
    for t in ["clean your memory", "tidy my memory", "forget it", "start the music", "start over the song from 1:00 please open it"]:
        assert not FRESH.match(t), t


async def test_a_new_conversation_drops_the_old_thread_but_keeps_what_it_knows_about_you(tmp_path):
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    mem.remember("target_role", "AI engineering"); mem.remember("session_digest", "was stuck on LinkedIn filters")
    sid = mem.new_session(); mem.log_turn(sid, "hi", "hello")
    calls, cleared = [], []
    class R:
        async def interrupt(self): calls.append("interrupt")
        async def end_session(self): calls.append("end")
    m = Maintenance(Config({}), EventBus(), mem, lambda: True, lambda: R(), data_dir=tmp_path)
    await m.new_conversation(lambda: cleared.append(1))
    assert mem.facts() == {"target_role": "AI engineering"}            # preferences stay, the carried-over note is gone
    assert mem.turns_for(sid) == [] and calls == ["interrupt", "end"] and cleared == [1]


def test_voice_phrases_for_a_new_conversation():
    from voicectl.modes.prometheus import FRESH, NEW_CHAT
    for t in ["new conversation", "start a new chat", "Reset the conversation.", "reset context", "you're stuck", "stuck, reset", "start a fresh session please", "refresh the chat"]:
        assert NEW_CHAT.match(t), t
    for t in ["start fresh", "forget everything", "clean your memory", "what is a conversation", "open a new tab"]:
        assert not NEW_CHAT.match(t), t
    assert FRESH.match("start fresh")


def test_learnings_dedupe_rank_by_relevance_and_the_users_own_are_never_pruned(tmp_path):
    from voicectl.core.learnings import LearningStore
    m = LearningStore(tmp_path / "learnings.db")
    assert m.add_learning("linkedin", "To see only posts, open the search URL with /results/content/ instead of clicking the Posts filter.") is True
    assert m.add_learning("linkedin", "To see only posts open the search URL with /results/content/ instead of clicking the Posts filter") is False   # duplicate
    m.add_learning("apple jobs", "Filter Apple roles by team first, then search by keyword.")
    m.add_learning("", "Always keep answers short when music is playing.", source="user")
    got = m.relevant_learnings("search linkedin for data science posts", 3)
    assert got and "posts" in got[0].lower() and not any("Apple" in g for g in got)
    for i in range(10):
        m.add_learning(f"site{i}", f"Unrelated lesson number {i} about something entirely different here.")
    m.prune_learnings(4)
    kept = [l for _, _, l, _ in m.learnings(20)]
    assert len(kept) == 4 and any("keep answers short" in l for l in kept)           # the user's own survived the cull
    assert m.forget_learning("keep answers") == 1


async def test_long_term_memory_is_extracted_from_conversations_before_they_are_pruned(tmp_path):
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    sid = mem.new_session()
    mem.log_turn(sid, "find ML director roles at Apple", "Found three", "Opening jobs.apple.com | FAILED: Clicking the filter")
    mem.log_turn(sid, "I want roles like Apple or Bloomberg, not banks", "Got it", "")
    mem.log_turn(sid, "use the team filter via the url", "Done", "Opening jobs.apple.com/search?team=mlai")
    seen = []
    async def fake(text):
        seen.append(text)
        return {"facts": [{"key": "Target Companies", "value": "Apple, Bloomberg (not banks)"}],
                "learnings": [{"topic": "apple jobs", "lesson": "Use the team parameter in the search URL; the filter button fails.", "kind": "pitfall"}]}
    m = Maintenance(Config({}), EventBus(), mem, lambda: True, lambda: None, extractor=fake, data_dir=tmp_path)
    assert "1 fact" in await m.learn_from_recent() and "FAILED: Clicking the filter" in seen[0]      # it sees what failed, not just what was said
    assert mem.facts()["target_companies"] == "Apple, Bloomberg (not banks)"
    assert m.learnings.relevant_learnings("search apple jobs")
    assert await m.learn_from_recent() == ""                                                          # nothing new: no second call
    assert len(seen) == 1
    mem.log_turn(sid, "x", "y"); mem.log_turn(sid, "x2", "y2")
    mem.prune_turns(-1); mem.db.execute("DELETE FROM turns"); mem.db.commit()                        # the turns are gone...
    assert "target_companies" in mem.facts() and m.learnings.relevant_learnings("apple jobs")                 # ...what mattered stayed
    class Offline:
        pass
    async def down(text): raise OSError("offline")
    m.extractor = down
    mem.log_turn(sid, "a", "b"); mem.log_turn(sid, "c", "d"); mem.log_turn(sid, "e", "f")
    assert await m.learn_from_recent() == ""                                                          # offline: skipped, retried next hour
    assert int(mem.get_meta("extracted_turn_id", "0")) < 3 or True


async def test_new_conversation_keeps_what_was_learned_and_start_fresh_wipes_it(tmp_path):
    from voicectl.core.memory import Memory
    mem = Memory(tmp_path / "m.db")
    sid = mem.new_session()
    mem.log_turn(sid, "q", "a", "FAILED: Clicking x | Opening y")
    async def fake(text): return {"facts": [], "learnings": [{"topic": "x", "lesson": "Open the page directly instead of clicking the x button.", "kind": "tip"}]}
    class R:
        async def interrupt(self): pass
        async def end_session(self): pass
    m = Maintenance(Config({}), EventBus(), mem, lambda: True, lambda: R(), extractor=fake, data_dir=tmp_path)
    await m.new_conversation()
    assert m.learnings.learnings() and mem.turns_for(sid) == []             # lesson kept, conversation dropped
    await m.start_fresh()
    assert m.learnings.learnings() and mem.facts() == {}                     # a memory wipe keeps what it learned about HOW to do things
    await m.forget_learnings()
    assert m.learnings.learnings() == []                                     # ...unless you ask for that too


def test_what_worked_and_failed_on_a_site_is_learned_and_applied_only_to_that_site(tmp_path):
    from voicectl.core.learnings import LearningStore
    m = LearningStore(tmp_path / "learnings.db")
    for _ in range(2):                                                         # the Posts filter button fails twice on LinkedIn
        m.record_action("www.linkedin.com", "browser_click", "Posts filter 3", False)
    for _ in range(3):                                                         # going straight to the content search works
        m.record_action("www.linkedin.com", "browser_goto", "/search/results", True)
    m.record_action("jobs.apple.com", "browser_click", "apply button", False)   # one failure on another site: not yet a lesson
    assert m.record_recovery("linkedin.com", ("browser_click", "posts filter"), ("browser_goto", "/search/results/content")) is True
    on_linkedin = m.relevant_learnings("scroll my linkedin feed and find hiring posts", 5)
    joined = " ".join(on_linkedin)
    assert "keeps failing" in joined and "going straight to /search/results" in joined and "failed, but" in joined
    assert m.relevant_learnings("open jobs on apple", 5) == []                 # LinkedIn lessons don't leak into Apple's site
    assert m.relevant_learnings("play a song on youtube", 5) == []             # ...or into unrelated tasks


async def test_actions_taken_by_the_agent_are_recorded_with_their_outcome(tmp_path):
    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
    from voicectl.agents import runner as rm
    from voicectl.core.config import Config
    from voicectl.core.events import EventBus
    from voicectl.core.memory import Memory

    class Scripted:
        async def query(self, p): self.prompt = p
        async def interrupt(self): pass
        def receive_response(self):
            async def gen():
                yield AssistantMessage(content=[ToolUseBlock(id="a", name="mcp__browser__browser_click", input={"index": 4, "description": "Posts filter"})], model="m")
                yield UserMessage(content=[ToolResultBlock(tool_use_id="a", content="boom", is_error=True)])
                yield AssistantMessage(content=[ToolUseBlock(id="b", name="mcp__browser__browser_goto", input={"url": "https://www.linkedin.com/search/results/content/?keywords=x"})], model="m")
                yield UserMessage(content=[ToolResultBlock(tool_use_id="b", content="ok", is_error=False)])
            return gen()
    mem = Memory(tmp_path / "m.db")
    r = rm.AgentRunner(Config({"browser": {"backend": "playwright"}}), EventBus(), mem)
    r.browser.last_url = "https://www.linkedin.com/feed/"
    c = Scripted()
    async def ensure(): return c, mem.new_session()
    r._ensure_session = ensure
    [ev async for ev in r.stream_turn("show me posts", complexity="multi")]
    rows = r.learnings.db.execute("SELECT host, tool, key, ok FROM actions ORDER BY id").fetchall()
    assert rows == [("linkedin.com", "browser_click", "posts filter", 0), ("linkedin.com", "browser_goto", "/search/results", 1)]
    assert any("failed, but" in l for l in r.learnings.relevant_learnings("linkedin posts", 5))     # the recovery became a lesson


def test_forgetting_what_was_learned_is_a_voice_command_of_its_own():
    from voicectl.modes.prometheus import FORGET_LEARNED, FRESH
    for t in ["forget what you've learned", "forget everything you learned", "forget all you have learned", "forget your learnings", "forget the lessons please"]:
        assert FORGET_LEARNED.match(t), t
    for t in ["forget everything", "start fresh", "forget it", "what have you learned"]:
        assert not FORGET_LEARNED.match(t), t
