from voicectl.agents.runner import describe_tool, needs_approval
from voicectl.core.memory import Memory


def test_safe_steps_not_gated():
    assert needs_approval("mcp__browser__browser_click", {"index": 3, "description": "Open the first job listing"}) is None
    assert needs_approval("mcp__browser__browser_type", {"index": 1, "text": "product manager", "submit": True, "description": "Search box"}) is None


def test_risky_steps_gated():
    assert needs_approval("mcp__browser__browser_click", {"index": 9, "description": "Easy Apply button"})
    assert needs_approval("mcp__browser__browser_click", {"index": 2, "description": "Send message"})
    assert needs_approval("Bash", {"command": "rm -rf ~/x"})
    assert needs_approval("Write", {"file_path": "/tmp/a"})


def test_describe():
    assert describe_tool("mcp__browser__browser_goto", {"url": "linkedin.com"}) == "Opening linkedin.com"


def test_memory_session_lifecycle(tmp_path):
    m = Memory(tmp_path / "m.db")
    assert m.active_session(60) is None
    sid = m.new_session()
    m.record_turn(sid, "abc")
    row = m.active_session(60)
    assert row["claude_session_id"] == "abc" and row["turns"] == 1
    m.end_sessions()
    assert m.active_session(60) is None          # idle/ended -> new session next time
    m.remember("role", "PM"); assert m.facts() == {"role": "PM"}
    assert m.forget("role") and m.facts() == {}
