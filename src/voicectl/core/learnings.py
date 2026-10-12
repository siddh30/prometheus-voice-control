from __future__ import annotations

"""Long-term learnings: what has been learned about HOW to do things (which site quirks bite, which method works), kept in its own
store (`learnings.db`), apart from the conversation memory. Why apart: it is knowledge about the web and the tools, not about the
user or any one conversation, so it survives "start fresh", can be inspected or deleted on its own, and can grow without touching
the conversation database.

Three tables: `actions` (every browser/mouse action and whether it worked), `learnings` (the distilled lessons) and `routes` (a whole
task: its goal, what failed, and the route that worked instead). A pattern that keeps failing, one that keeps working, or a failure
followed by a different method that worked, is promoted to a lesson; a task that hit failures and still succeeded becomes a route, which
the next similar task is shown before it starts (in the same conversation or a later one)."""

import difflib
import json
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
            CREATE TABLE IF NOT EXISTS routes(
                id INTEGER PRIMARY KEY, host TEXT, goal TEXT, words TEXT, failed TEXT, worked TEXT,
                hits INTEGER DEFAULT 1, created REAL, last_used REAL);
            CREATE TABLE IF NOT EXISTS outcomes(
                id INTEGER PRIMARY KEY, goal TEXT, words TEXT, rating TEXT, note TEXT, how TEXT, mode TEXT,
                hits INTEGER DEFAULT 1, created REAL, last_used REAL);
        """)
        self.db.commit()

    @classmethod
    def beside(cls, memory) -> "LearningStore":
        """The store that sits next to a conversation memory file."""
        return cls(Path(memory.path).with_name("learnings.db"))

    def clear(self) -> None:
        self.db.executescript("DELETE FROM learnings; DELETE FROM actions; DELETE FROM routes; DELETE FROM outcomes;")
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

    # --- routes: a whole task that hit failures and still got there ("to find posts on LinkedIn: the filter failed, the search URL worked") ---
    GOAL_STOP = {"the", "and", "for", "with", "that", "this", "can", "you", "please", "open", "show", "get", "find", "let", "now", "just", "then",
                 "from", "into", "have", "has", "was", "are", "not", "but", "all", "any", "out"}

    @classmethod
    def _goal_words(cls, text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())} - cls.GOAL_STOP

    @staticmethod
    def _clean_host(host: str) -> str:
        return (host or "").lower().removeprefix("www.")

    def record_route(self, host: str, goal: str, failed: list[tuple[str, str]], worked: list[tuple[str, str]]) -> bool:
        """A task whose first method(s) failed and which then succeeded by another route. A similar goal on the same site strengthens the
        existing route (and keeps the shortest route that worked); a new one is added. True if it was new."""
        host, goal = self._clean_host(host), re.sub(r"\s+", " ", goal.strip())[:140]
        words = self._goal_words(goal)
        if not host or not failed or not worked or len(words) < 2:
            return False
        norm = lambda step: (step[0], re.sub(r"\d+", "#", (step[1] or "").lower().strip())[:60])    # the same spelling the action log uses
        failed = list(dict.fromkeys(norm(s) for s in failed))[:4]
        worked = [norm(s) for s in worked]
        worked = [s for i, s in enumerate(worked) if i == 0 or s != worked[i - 1]][:8]                 # no stuttering repeats
        now = time.time()
        for rid, rwords, rfailed, rworked in self.db.execute("SELECT id, words, failed, worked FROM routes WHERE host = ?", (host,)).fetchall():
            old = set(rwords.split())
            if len(words & old) / len(words | old) >= 0.6:
                merged = list(dict.fromkeys([*(tuple(x) for x in json.loads(rfailed)), *failed]))[:4]
                best = worked if len(worked) <= len(json.loads(rworked)) else json.loads(rworked)
                self.db.execute("UPDATE routes SET hits = hits + 1, last_used = ?, failed = ?, worked = ? WHERE id = ?",
                                (now, json.dumps(merged), json.dumps(best), rid))
                self.db.commit()
                return False
        self.db.execute("INSERT INTO routes(host, goal, words, failed, worked, hits, created, last_used) VALUES(?,?,?,?,?,1,?,?)",
                        (host, goal, " ".join(sorted(words)), json.dumps(failed), json.dumps(worked), now, now))
        self.db.commit()
        return True

    def _describe(self, steps: list[tuple[str, str]], limit: int) -> str:
        return ", then ".join(f"{self.VERB.get(t, t)} '{k}'" for t, k in steps[:limit])

    def relevant_routes(self, text: str, host: str = "", n: int = 2) -> list[str]:
        """The saved routes that fit this task (similar goal; the same site counts for more), as short instructions. Marks them used."""
        words, host = self._goal_words(text), self._clean_host(host)
        if not words:
            return []
        scored = []
        for rid, rhost, goal, rwords, failed, worked, hits in self.db.execute("SELECT id, host, goal, words, failed, worked, hits FROM routes").fetchall():
            old = set(rwords.split())
            shared = len(words & old)
            jaccard = shared / len(words | old)
            same_site = bool(host) and (host == rhost or rhost in text.lower() or rhost.split(".")[0] in text.lower())
            if (shared >= 2 and jaccard >= 0.34) or (same_site and shared >= 1 and jaccard >= 0.2):
                scored.append((jaccard + (0.3 if same_site else 0) + min(hits, 5) * 0.03, rid, rhost, goal, json.loads(failed), json.loads(worked), hits))
        scored.sort(reverse=True)
        out, used = [], []
        for _, rid, rhost, goal, failed, worked, hits in scored[:n]:
            out.append(f"On {rhost}, for a task like \"{goal}\": {self._describe(failed, 3)} failed. What worked instead: {self._describe(worked, 6)}"
                       f"{f' (worked {hits} times)' if hits > 1 else ''}.")
            used.append((time.time(), rid))
        if used:
            self.db.executemany("UPDATE routes SET last_used = ?, hits = hits + 1 WHERE id = ?", used)
            self.db.commit()
        return out

    # --- outcomes: the user's thumbs up / down on whole runs ("this went well, done this way" / "this went badly, and why") ---
    def record_outcome(self, goal: str, rating: str, note: str = "", how: str = "", mode: str = "") -> bool:
        """A rated run. A similar goal rated the same way strengthens the old entry; rated the other way, the newest verdict replaces it
        (people change their minds, and the latest judgement is the one to follow). True if a new entry was added."""
        goal, note, how = re.sub(r"\s+", " ", goal.strip())[:140], note.strip()[:240], how.strip()[:400]
        words = self._goal_words(goal)
        if rating not in ("up", "down") or len(words) < 2:
            return False
        now = time.time()
        for oid, owords, orating in self.db.execute("SELECT id, words, rating FROM outcomes").fetchall():
            old = set(owords.split())
            if len(words & old) / len(words | old) < 0.6:
                continue
            if orating == rating:
                self.db.execute("UPDATE outcomes SET hits = hits + 1, last_used = ?, note = CASE WHEN ? != '' THEN ? ELSE note END, "
                                "how = CASE WHEN ? != '' THEN ? ELSE how END WHERE id = ?", (now, note, note, how, how, oid))
                self.db.commit()
                return False
            self.db.execute("DELETE FROM outcomes WHERE id = ?", (oid,))
        self.db.execute("INSERT INTO outcomes(goal, words, rating, note, how, mode, hits, created, last_used) VALUES(?,?,?,?,?,?,1,?,?)",
                        (goal, " ".join(sorted(words)), rating, note, how, mode, now, now))
        self.db.commit()
        return True

    def relevant_outcomes(self, text: str, n: int = 3) -> list[str]:
        """What the user said about similar runs before, as short instructions: repeat what they liked, avoid what they didn't."""
        words = self._goal_words(text)
        if not words:
            return []
        scored = []
        for oid, goal, owords, rating, note, how, hits in self.db.execute("SELECT id, goal, words, rating, note, how, hits FROM outcomes").fetchall():
            old = set(owords.split())
            shared, jaccard = len(words & old), len(words & old) / len(words | old)
            if shared >= 2 and jaccard >= 0.34:
                scored.append((jaccard + min(hits, 5) * 0.03 + (0.05 if rating == "down" else 0), oid, goal, rating, note, how))
        scored.sort(reverse=True)
        out, used = [], []
        for _, oid, goal, rating, note, how in scored[:n]:
            if rating == "up":
                out.append(f"The user was happy with a similar task, \"{goal}\"{f': {how}' if how else ''}. Do it that way again.")
            else:
                out.append(f"The user was NOT happy with a similar task, \"{goal}\"{f': {how}' if how else ''}."
                           f"{f' They said: {note}.' if note else ''} Do not repeat that; try a different approach.")
            used.append((time.time(), oid))
        if used:
            self.db.executemany("UPDATE outcomes SET last_used = ?, hits = hits + 1 WHERE id = ?", used)
            self.db.commit()
        return out

    def outcome_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT rating, COUNT(*) FROM outcomes GROUP BY rating").fetchall())

    def prune_outcomes(self, max_n: int) -> int:
        rows = self.db.execute("SELECT id, hits, last_used FROM outcomes").fetchall()
        if len(rows) <= max_n:
            return 0
        now = time.time()
        drop = sorted(rows, key=lambda r: r[1] * 0.5 ** ((now - r[2]) / (60 * 86400)))[: len(rows) - max_n]
        self.db.executemany("DELETE FROM outcomes WHERE id = ?", [(r[0],) for r in drop])
        self.db.commit()
        return len(drop)

    def routes(self, limit: int = 30) -> list[tuple[int, str, str]]:
        return self.db.execute("SELECT id, host, goal FROM routes ORDER BY hits DESC, last_used DESC LIMIT ?", (limit,)).fetchall()

    def prune_routes(self, max_n: int) -> int:
        """Keep the most useful `max_n` routes (used often, used recently)."""
        rows = self.db.execute("SELECT id, hits, last_used FROM routes").fetchall()
        if len(rows) <= max_n:
            return 0
        now = time.time()
        ranked = sorted(rows, key=lambda r: r[1] * 0.5 ** ((now - r[2]) / (30 * 86400)))
        drop = ranked[: len(rows) - max_n]
        self.db.executemany("DELETE FROM routes WHERE id = ?", [(r[0],) for r in drop])
        self.db.commit()
        return len(drop)

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
            routes = self.db.execute("DELETE FROM routes WHERE lower(goal) LIKE ? OR lower(host) LIKE ?", (f"%{q}%", f"%{q}%"))
            self.db.commit()
            return cur.rowcount + routes.rowcount
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
