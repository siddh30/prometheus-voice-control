from __future__ import annotations

"""Long-term learnings: what has been learned about HOW to do things (which site quirks bite, which method works), kept in its own
store (`learnings.db`), apart from the conversation memory. Why apart: it is knowledge about the web and the tools, not about the
user or any one conversation, so it survives "start fresh", can be inspected or deleted on its own, and can grow without touching
the conversation database.

Two tables: `actions` (every browser/mouse action and whether it worked) and `learnings` (the distilled lessons). A pattern that
keeps failing, one that keeps working, or a failure followed by a different method that worked, is promoted to a lesson."""

import difflib
import re
import sqlite3
import time
from pathlib import Path


class LearningStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS learnings(
                id INTEGER PRIMARY KEY, topic TEXT, lesson TEXT, kind TEXT DEFAULT 'tip', source TEXT DEFAULT 'auto',
                hits INTEGER DEFAULT 1, created REAL, last_used REAL);
            CREATE TABLE IF NOT EXISTS actions(
                id INTEGER PRIMARY KEY, ts REAL, host TEXT, tool TEXT, key TEXT, ok INTEGER);
        """)
        self.db.commit()

    @classmethod
    def beside(cls, memory) -> "LearningStore":
        """The store that sits next to a conversation memory file."""
        return cls(Path(memory.path).with_name("learnings.db"))

    def clear(self) -> None:
        self.db.executescript("DELETE FROM learnings; DELETE FROM actions;")
        self.db.commit()

    # --- learnings: lessons from experience ("on LinkedIn, filter posts via the search URL, not the filter button") ---
    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()

    def add_learning(self, topic: str, lesson: str, kind: str = "tip", source: str = "auto") -> bool:
        """Save a lesson. A near-duplicate strengthens the existing one instead of adding another. True if it was new."""
        lesson, topic = lesson.strip()[:240], topic.strip()[:60].lower()
        if len(lesson) < 12:
            return False
        norm, now = self._norm(lesson), time.time()
        for lid, old in self.db.execute("SELECT id, lesson FROM learnings").fetchall():
            if difflib.SequenceMatcher(None, norm, self._norm(old)).ratio() >= 0.8:
                self.db.execute("UPDATE learnings SET hits = hits + 1, last_used = ?, source = CASE WHEN ? = 'user' THEN 'user' ELSE source END "
                                "WHERE id = ?", (now, source, lid))
                self.db.commit()
                return False
        self.db.execute("INSERT INTO learnings(topic, lesson, kind, source, hits, created, last_used) VALUES(?,?,?,?,1,?,?)",
                        (topic, lesson, kind, source, now, now))
        self.db.commit()
        return True

    # --- action outcomes: every browser/mouse action is recorded, and what keeps failing or keeps working becomes a learning ---
    VERB = {"browser_click": "clicking", "browser_type": "typing into", "browser_goto": "opening", "browser_new_tab": "opening",
            "screen_click": "mouse-clicking", "browser_scroll": "scrolling", "browser_switch_tab": "switching to"}

    def record_action(self, host: str, tool: str, key: str, ok: bool) -> None:
        """One action and whether it worked. After enough evidence the pattern is promoted to a learning (no model call needed)."""
        host, key = (host or "").lower().removeprefix("www."), re.sub(r"\d+", "#", (key or "").lower().strip())[:60]
        if not host or not key:
            return
        self.db.execute("INSERT INTO actions(ts, host, tool, key, ok) VALUES(?,?,?,?,?)", (time.time(), host, tool, key, int(ok)))
        self.db.commit()
        okn, failn = self.db.execute("SELECT COALESCE(SUM(ok),0), COALESCE(SUM(1-ok),0) FROM actions WHERE host=? AND tool=? AND key=?",
                                     (host, tool, key)).fetchone()
        verb = self.VERB.get(tool, tool)
        if failn >= 2 and okn == 0:
            self.add_learning(host, f"On {host}, {verb} '{key}' keeps failing; use another method (the direct URL, or the mouse).", "pitfall")
        elif tool in ("browser_goto", "browser_new_tab") and okn >= 3 and failn == 0:
            self.add_learning(host, f"On {host}, going straight to {key} works every time; skip clicking through.", "tip")

    def record_recovery(self, host: str, failed: tuple[str, str], worked: tuple[str, str]) -> bool:
        """Within one task, one method failed and a different one worked: the most useful lesson there is."""
        host = (host or "").lower().removeprefix("www.")
        if not host or failed == worked:
            return False
        v = self.VERB
        return self.add_learning(host, f"On {host}: {v.get(failed[0], failed[0])} '{failed[1]}' failed, but {v.get(worked[0], worked[0])} '{worked[1]}' worked.", "tip")

    def prune_actions(self, older_than_s: float = 30 * 86400) -> int:
        cur = self.db.execute("DELETE FROM actions WHERE ts < ?", (time.time() - older_than_s,))
        self.db.commit()
        return cur.rowcount

    def relevant_learnings(self, text: str, n: int = 5) -> list[str]:
        """The lessons that apply to this request (by shared words with the topic or lesson), strongest first. Marks them used."""
        words = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())} - {"the", "and", "for", "with", "that", "this", "open", "can", "you"}
        scored = []
        for lid, topic, lesson, hits, last in self.db.execute("SELECT id, topic, lesson, hits, last_used FROM learnings").fetchall():
            overlap = len(words & set(re.findall(r"[a-z0-9]{3,}", f"{topic} {lesson}".lower())))
            stems = [t for t in re.findall(r"[a-z0-9]+", topic) if t not in {"com", "org", "net", "io", "www", "co", "app"} and len(t) > 2]
            topic_hit = 3 if stems and any(t in text.lower() for t in stems) else 0     # "linkedin.com" applies when LinkedIn is in play
            if overlap >= 2 or topic_hit:                      # one shared word ("search") is not relevance
                scored.append((overlap + topic_hit + min(hits, 5) * 0.2, lid, lesson))
        scored.sort(reverse=True)
        top = scored[:n]
        if top:
            self.db.executemany("UPDATE learnings SET last_used = ?, hits = hits + 1 WHERE id = ?", [(time.time(), lid) for _, lid, _ in top])
            self.db.commit()
        return [lesson for _, _, lesson in top]

    def learnings(self, limit: int = 50) -> list[tuple[int, str, str, str]]:
        return self.db.execute("SELECT id, topic, lesson, source FROM learnings ORDER BY hits DESC, last_used DESC LIMIT ?", (limit,)).fetchall()

    def forget_learning(self, query: str) -> int:
        q = query.strip().lower()
        if q.isdigit():
            cur = self.db.execute("DELETE FROM learnings WHERE id = ?", (int(q),))
        else:
            cur = self.db.execute("DELETE FROM learnings WHERE lower(lesson) LIKE ? OR lower(topic) LIKE ?", (f"%{q}%", f"%{q}%"))
        self.db.commit()
        return cur.rowcount

    def prune_learnings(self, max_n: int) -> int:
        """Keep the most useful `max_n` (used often, used recently). Lessons the user told us are never pruned."""
        rows = self.db.execute("SELECT id, hits, last_used, source FROM learnings").fetchall()
        if len(rows) <= max_n:
            return 0
        now = time.time()
        auto = sorted((r for r in rows if r[3] != "user"), key=lambda r: r[1] * 0.5 ** ((now - r[2]) / (30 * 86400)))
        drop = auto[: len(rows) - max_n]
        self.db.executemany("DELETE FROM learnings WHERE id = ?", [(r[0],) for r in drop])
        self.db.commit()
        return len(drop)
