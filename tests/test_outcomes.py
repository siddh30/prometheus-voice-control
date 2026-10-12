"""Thumbs up / down become knowledge: a similar request later is told what the user liked and what they did not."""
import asyncio

from voicectl import llm
from voicectl.core.events import EventBus
from voicectl.core.feedback import FeedbackLog
from voicectl.core.learnings import LearningStore
from voicectl.llm import ToolCall
from voicectl.swarm.router import PC, Router

GOAL = "open my linkedin profile and read the headline"


def store(tmp_path):
    return LearningStore(tmp_path / "learnings.db")


def test_a_liked_run_is_recommended_and_a_disliked_one_is_warned_against(tmp_path):
    s = store(tmp_path)
    s.record_outcome(GOAL, "up", how="browser_goto linkedin.com/in/me, then read the page")
    s.record_outcome("find posts about product management on linkedin", "down", note="it used the filter button and got stuck")
    good = s.relevant_outcomes("open my linkedin profile and read the about section")
    assert len(good) == 1 and "happy" in good[0] and "linkedin.com/in/me" in good[0]
    bad = s.relevant_outcomes("find linkedin posts about product management")
    assert len(bad) == 1 and "NOT happy" in bad[0] and "filter button" in bad[0] and "Do not repeat" in bad[0]
    assert s.relevant_outcomes("play some lofi on youtube") == []                       # unrelated requests get nothing


def test_the_newest_verdict_on_a_similar_goal_wins(tmp_path):
    s = store(tmp_path)
    s.record_outcome(GOAL, "up", how="way one")
    s.record_outcome(GOAL, "down", note="not like that")
    assert s.outcome_counts() == {"down": 1}
    s.record_outcome(GOAL, "down", note="still no")
    assert s.outcome_counts() == {"down": 1}                                             # the same verdict strengthens, never duplicates
    assert "still no" in s.relevant_outcomes(GOAL)[0]


def test_a_rating_without_a_real_goal_is_ignored_and_the_store_can_be_cleared_and_pruned(tmp_path):
    s = store(tmp_path)
    assert s.record_outcome("ok", "up") is False and s.record_outcome(GOAL, "meh") is False
    for i in range(5):
        s.record_outcome(f"open the report number{i} about quarterly revenue{i} and email it", "up")
    assert s.prune_outcomes(3) == 2 and sum(s.outcome_counts().values()) == 3
    s.clear()
    assert s.outcome_counts() == {}


def test_saving_a_rating_teaches_the_store_straight_away(tmp_path):
    s = store(tmp_path)
    f = FeedbackLog(EventBus(), tmp_path / "feedback.jsonl",
                    learn=lambda e: s.record_outcome(e["request"], e["rating"], e["note"], FeedbackLog.how(e), e.get("mode", "")))
    f.observe("transcript", {"text": GOAL, "mode": "prometheus"})
    f.observe("step", {"text": "browser_goto linkedin.com/in/me"})
    f.observe("step", {"text": "a slip", "failed": True})
    f.observe("step", {"text": "browser_read the page"})
    assert f.record("up")
    (line,) = s.relevant_outcomes(GOAL)
    assert "browser_goto linkedin.com/in/me, then browser_read the page" in line and "a slip" not in line


def test_a_failing_learner_never_loses_the_rating(tmp_path):
    def broken(_): raise RuntimeError("db locked")
    f = FeedbackLog(EventBus(), tmp_path / "feedback.jsonl", learn=broken)
    f.observe("transcript", {"text": GOAL, "mode": "prometheus"})
    assert f.record("up") is True and len(f.entries()) == 1


def test_the_swarm_router_is_told_what_the_user_said_about_similar_runs(monkeypatch):
    seen = {}
    class Fake:
        def tool_calls(self, **kw):
            seen["system"] = kw["system"]
            return [ToolCall("assign", {"assignments": [{"pc": "Studio", "task": "a b"}]})]
    monkeypatch.setattr(llm, "current", lambda: Fake())
    pcs = [PC("Studio", local=True), PC("MacBook")]
    asyncio.run(Router(pcs).route("open gmail and play music", hint="- the user liked it when music ran on the Studio"))
    assert "liked it when music ran on the Studio" in seen["system"]
