from __future__ import annotations

"""How big is this request? That decides how much planning to show, so it is consistent instead of the model's mood:

  none      plain conversation                           -> just answer
  single    one step (one page action, one lookup)       -> act, no checklist
  multi     a few steps                                  -> short checklist
  extended  many steps / several sources / consequential -> a spoken plan + checklist before starting

Jev answers this in the same call that detects instant commands (no extra latency); the heuristic below is the fallback.
"""

import re
from dataclasses import dataclass

LEVELS = ("none", "single", "multi", "extended")


@dataclass(frozen=True)
class Complexity:
    level: str
    source: str                      # "jev" | "heuristic" | "pattern"
    confidence: float | None = None


TASK_VERB = re.compile(r"\b(open|play|search|find|look up|click|scroll|go to|type|send|apply|buy|order|book|check|read|"
                       r"show|get|download|compare|rank|research|analy[sz]e|summari[sz]e|write|draft|fill|post|pay|"
                       r"look at|select|navigate|set up|create|pull up)\b", re.I)
QUESTION_START = re.compile(r"^\s*(what|why|how|who|when|where|which|is|are|do|does|did|can you tell|could you tell|tell me|"
                            r"explain|define)\b", re.I)
CONSEQUENTIAL = re.compile(r"\b(apply|send|email|message|buy|purchase|order|book|pay|post|publish|submit|sign up|subscribe|delete|"
                           r"remove|checkout)\b", re.I)
BROAD = re.compile(r"\b(compare|rank|research|analy[sz]e|summari[sz]e|shortlist|each|every|all of|several|top \d+|best \d+|"
                   r"pros and cons|resume|cover letter|jobs?|candidates?)\b", re.I)
SEQUENCE = re.compile(r"\b(and then|then|after that|afterwards|next|finally|followed by|once that)\b", re.I)


def heuristic(text: str) -> Complexity:
    words = len(text.split())
    seq = len(SEQUENCE.findall(text))
    verbs = len(TASK_VERB.findall(text))
    if verbs == 0 and (QUESTION_START.match(text) or text.rstrip().endswith("?") or words <= 4):
        return Complexity("none", "heuristic")
    if CONSEQUENTIAL.search(text) and verbs >= 1 and (words >= 8 or seq):
        return Complexity("extended", "heuristic")
    if BROAD.search(text) and verbs >= 1 and words >= 7 or seq >= 2 or words >= 28:
        return Complexity("extended", "heuristic")
    if seq >= 1 or words >= 12 or verbs >= 3 or re.search(r"\b\w+ and (?:then )?(?:play|click|open|search|find|read)\b", text, re.I):
        return Complexity("multi", "heuristic")
    return Complexity("single", "heuristic")


HINTS = {
    "none": "Plain conversation. Answer in a sentence or two. No tools and no checklist.",
    "single": "A single-step request. Say one short sentence, do it, report back. Do NOT create a checklist.",
    "multi": ("A few steps. Say in one sentence what you will do, call checklist_set with 3 to 5 short steps and a one-sentence "
              "summary of the goal, then work through them, updating the checklist as you go."),
    "extended": ("A big task. FIRST say the plan aloud in two short sentences (the goal and the main stages), and call "
                 "checklist_set with 4 to 8 steps plus a one-sentence summary so the user can see the whole plan. Then go through "
                 "it, updating the checklist. Anything consequential (sending, applying, buying, deleting) still waits for the "
                 "user's yes."),
}


def hint_block(c: Complexity) -> str:
    return f'<TaskComplexity level="{c.level}">{HINTS[c.level]}</TaskComplexity>'
