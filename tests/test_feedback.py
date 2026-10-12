import asyncio
import json

from voicectl.api import parse_command
from voicectl.core.events import EventBus
from voicectl.core.feedback import FeedbackLog


def make(tmp_path):
    return FeedbackLog(EventBus(), tmp_path / "feedback.jsonl", clock=lambda: 1_700_000_000)


def test_the_command_needs_a_known_rating():
    assert parse_command({"type": "feedback", "rating": "up"}).get("rating") == "up"
    assert parse_command({"type": "feedback", "rating": "down", "note": "wrong tab"}).get("note") == "wrong tab"
    assert parse_command({"type": "feedback", "rating": "meh"}) is None
    assert parse_command({"type": "feedback"}) is None


def test_a_rating_is_saved_with_the_run_it_belongs_to(tmp_path):
    f = make(tmp_path)
    f.observe("transcript", {"text": "open gmail", "mode": "prometheus"})
    f.observe("route", {"kind": "agent", "how": "llm"})
    f.observe("step", {"text": "browser_goto gmail.com"})
    f.observe("result", {"text": "Gmail is open."})
    assert f.record("up", "fast") is True
    (e,) = f.entries()
    assert e["rating"] == "up" and e["note"] == "fast" and e["request"] == "open gmail" and e["mode"] == "prometheus"
    assert e["result"] == "Gmail is open." and e["steps"][0]["text"].startswith("browser_goto") and e["route"]["how"] == "llm"


def test_the_swarm_run_keeps_what_each_computer_did(tmp_path):
    f = make(tmp_path)
    f.observe("transcript", {"text": "open gmail on the macbook", "mode": "swarm"})
    f.observe("swarm", {"pcs": [{"name": "MacBook", "state": "done", "task": "open gmail", "result": "opened"},
                                {"name": "Mac mini", "state": "idle", "task": "", "result": ""}], "routing": "named"})
    f.record("down", "it opened the wrong account")
    (e,) = f.entries("down")
    assert e["swarm"]["routing"] == "named" and [p["name"] for p in e["swarm"]["pcs"]] == ["MacBook"]      # idle computers are left out


def test_a_new_request_starts_a_new_run_and_nothing_to_rate_is_not_saved(tmp_path):
    f = make(tmp_path)
    assert f.record("up") is False                                        # nothing has run yet
    f.observe("transcript", {"text": "first", "mode": "prometheus"})
    f.observe("result", {"text": "one"})
    f.observe("transcript", {"text": "second", "mode": "prometheus"})
    f.record("up")
    assert f.entries()[0]["request"] == "second" and "result" not in f.entries()[0]
    assert f.record("sideways") is False


def test_a_rating_can_be_read_back_by_kind(tmp_path):
    f = make(tmp_path)
    for rating in ("up", "down", "up"):
        f.observe("transcript", {"text": f"job {rating}", "mode": "prometheus"})
        f.record(rating)
    assert len(f.entries("up")) == 2 and len(f.entries("down")) == 1 and len(f.entries()) == 3
    assert all(json.loads(line) for line in (tmp_path / "feedback.jsonl").read_text().splitlines())


async def test_it_follows_the_bus_by_itself(tmp_path):
    f = make(tmp_path)
    f.start()
    await asyncio.sleep(0)
    f.bus.publish("transcript", text="play lofi", mode="prometheus")
    f.bus.publish("result", text="Playing.")
    await asyncio.sleep(0.05)
    assert f.record("up") and f.entries()[0]["result"] == "Playing."
    f.stop()
