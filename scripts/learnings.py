#!/usr/bin/env python3
"""See, add and test what Prometheus has learned.

  scripts/learnings.py show                       counts, lessons, routes, rated runs (thumbs) and the last ratings
  scripts/learnings.py add "topic" "lesson"       teach it something by hand (saved as the user's own lesson)
  scripts/learnings.py check "what you would say" what a NEW run of that request would be told (lessons, routes, past runs)

Default data folder: the Swarm app's if it exists, else Voice Control's. Use --data DIR for another (e.g. the MacBook's)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from voicectl.core.learnings import LearningStore  # noqa: E402

SUPPORT = Path.home() / "Library" / "Application Support"


def data_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    for app in ("PrometheusSwarm", "VoiceControl"):
        if (SUPPORT / app / "data" / "learnings.db").exists():
            return SUPPORT / app / "data"
    sys.exit("No learnings.db found; pass --data DIR.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    add = sub.add_parser("add"); add.add_argument("topic"); add.add_argument("lesson")
    chk = sub.add_parser("check"); chk.add_argument("request")
    args = ap.parse_args()
    folder = data_dir(args.data)
    store = LearningStore(folder / "learnings.db")
    print(f"data: {folder}")

    if args.cmd == "add":
        print("added" if store.add_learning(args.topic, args.lesson, source="user") else "not added (too short, or it already exists: it was strengthened)")
    elif args.cmd == "check":
        for title, lines in (("Lessons", store.relevant_learnings(args.request)), ("Routes", store.relevant_routes(args.request)),
                             ("Past runs (your thumbs)", store.relevant_outcomes(args.request))):
            print(f"\n{title}: {'none' if not lines else ''}")
            for line in lines:
                print(f"  - {line}")
    else:
        print(f"\nratings: {store.outcome_counts() or 'none yet'}")
        print("\nlessons:")
        for lid, topic, lesson, source in store.learnings(50):
            print(f"  [{lid}] ({topic}, {source}) {lesson}")
        print("\nroutes:")
        for rid, host, goal in store.routes(20):
            print(f"  [{rid}] {host}: {goal}")
        log = folder / "feedback.jsonl"
        print("\nlast ratings:")
        for line in (log.read_text().splitlines()[-8:] if log.exists() else []):
            e = json.loads(line)
            print(f"  {e['at']} {'👍' if e['rating'] == 'up' else '👎'} {e['request'][:70]}" + (f"  — {e['note']}" if e.get("note") else ""))


if __name__ == "__main__":
    main()
