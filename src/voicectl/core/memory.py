from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Memory:
    """Agent-mode memory (Speak/Action never touch this).

    sessions: the active Claude session id + scratch state, resumed across F11 presses until idle timeout.
    facts:    long-term key/value notes (resume summary, preferences) pre-loaded into the system prompt.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(
                id INTEGER PRIMARY KEY, claude_session_id TEXT, state TEXT DEFAULT '{}',
                turns INTEGER DEFAULT 0, started REAL, last_used REAL);
            CREATE TABLE IF NOT EXISTS facts(key TEXT PRIMARY KEY, value TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS turns(
                id INTEGER PRIMARY KEY, session_id INTEGER, ts REAL, user_text TEXT, answer TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        if "steps" not in {r[1] for r in self.db.execute("PRAGMA table_info(turns)")}:
            self.db.execute("ALTER TABLE turns ADD COLUMN steps TEXT DEFAULT ''")     # what the agent did, for learning from outcomes
        self.db.commit()

    # --- sessions ---
    def active_session(self, idle_timeout_s: float) -> dict | None:
        row = self.db.execute("SELECT id, claude_session_id, state, turns, started, last_used FROM sessions "
                              "ORDER BY last_used DESC LIMIT 1").fetchone()
        if not row or time.time() - row[5] > idle_timeout_s:
            return None
        return {"id": row[0], "claude_session_id": row[1], "state": json.loads(row[2]), "turns": row[3], "started": row[4]}

    def new_session(self) -> int:
        now = time.time()
        cur = self.db.execute("INSERT INTO sessions(started, last_used) VALUES(?, ?)", (now, now))
        self.db.commit()
        return cur.lastrowid

    def record_turn(self, session_id: int, claude_session_id: str | None) -> None:
        self.db.execute("UPDATE sessions SET turns = turns + 1, last_used = ?, "
                        "claude_session_id = COALESCE(?, claude_session_id) WHERE id = ?",
                        (time.time(), claude_session_id, session_id))
        self.db.commit()

    def end_sessions(self) -> None:
        self.db.execute("UPDATE sessions SET last_used = 0")
        self.db.commit()

    # --- turn log (feeds the hourly digest; pruned by maintenance) ---
    def log_turn(self, session_row: int, user_text: str, answer: str, steps: str = "") -> None:
        self.db.execute("INSERT INTO turns(session_id, ts, user_text, answer, steps) VALUES(?,?,?,?,?)",
                        (session_row, time.time(), user_text[:500], answer[:800], steps[:600]))
        self.db.commit()

    def turns_after(self, turn_id: int, limit: int = 60) -> list[tuple[int, str, str, str]]:
        """(id, user, answer, steps) for turns newer than `turn_id`: the raw material for extracting long-term memory."""
        return self.db.execute("SELECT id, user_text, answer, COALESCE(steps,'') FROM turns WHERE id > ? ORDER BY id LIMIT ?",
                               (turn_id, limit)).fetchall()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()


    def turns_for(self, session_row: int, limit: int = 40) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT user_text, answer FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                               (session_row, limit)).fetchall()
        return rows[::-1]

    def recent_turns(self, limit: int = 12, max_age_s: float = 7 * 86400) -> list[tuple[str, str]]:
        """The latest exchanges across ALL sessions (a restart opens an empty session; the conversation it continues is an older one)."""
        rows = self.db.execute("SELECT user_text, answer FROM turns WHERE ts > ? ORDER BY id DESC LIMIT ?",
                               (time.time() - max_age_s, limit)).fetchall()
        return rows[::-1]

    def prune_turns(self, older_than_s: float) -> int:
        cur = self.db.execute("DELETE FROM turns WHERE ts < ?", (time.time() - older_than_s,))
        self.db.commit()
        return cur.rowcount

    def latest_session(self) -> dict | None:
        """The newest session row (even if idle), with its age."""
        row = self.db.execute("SELECT id, claude_session_id, turns, started, last_used FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if not row or not row[1] and not row[2]:
            return None
        return {"id": row[0], "claude_session_id": row[1], "turns": row[2], "started": row[3], "last_used": row[4]}

    def prune_facts(self, max_n: int, protected: set[str]) -> int:
        """Keep at most `max_n` facts: drop the oldest unprotected ones. Never touches protected keys."""
        rows = self.db.execute("SELECT key FROM facts ORDER BY updated ASC").fetchall()
        excess = len(rows) - max_n
        dropped = 0
        for (key,) in rows:
            if dropped >= excess:
                break
            if key not in protected:
                self.db.execute("DELETE FROM facts WHERE key = ?", (key,))
                dropped += 1
        self.db.commit()
        return dropped

    # --- facts ---
    def remember(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO facts(key, value, updated) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                        (key, value, time.time()))
        self.db.commit()

    def forget(self, key: str) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE key = ?", (key,))
        self.db.commit()
        return cur.rowcount > 0

    def facts(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM facts ORDER BY key").fetchall())

    def clear(self) -> None:
        self.db.executescript("DELETE FROM sessions; DELETE FROM facts; DELETE FROM turns; DELETE FROM meta;")
        self.db.commit()
