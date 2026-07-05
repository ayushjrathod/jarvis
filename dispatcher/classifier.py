"""Quick-vs-agentic routing (locked decision #2).

Heuristic first: imperative verbs or file/vault references mean agentic work;
question-shaped or short inputs are quick Q&A. An LLM tiebreak (Haiku, one
word) can be plugged in via `llm_tiebreak` once API credentials exist — the
current rules leave no ambiguous bucket, so Phase A ships heuristic-only.
"""

from __future__ import annotations

import re
from typing import Callable

QUESTION_STARTERS = {
    "what", "who", "whom", "whose", "when", "where", "why", "how", "which",
    "is", "are", "am", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "tell", "explain", "define", "convert",
}

ACTION_VERBS = {
    "summarize", "summarise", "write", "create", "update", "organize",
    "organise", "generate", "add", "make", "build", "refactor", "clean",
    "move", "delete", "remove", "rename", "review", "analyze", "analyse",
    "draft", "compile", "fix", "edit", "append", "save", "archive",
}

PATH_RE = re.compile(r"\b\S+\.(md|txt|py|json|yaml|yml|csv)\b|\b(vault|queue|areas|data|notes|briefs)/", re.I)


def classify(text: str, llm_tiebreak: Callable[[str], str] | None = None) -> str:
    t = text.strip().lower()
    words = re.findall(r"[a-z']+", t)
    if any(w in ACTION_VERBS for w in words) or PATH_RE.search(t):
        return "agentic"
    if t.endswith("?") or (words and words[0] in QUESTION_STARTERS):
        return "quick"
    if len(words) <= 9:
        return "quick"
    if llm_tiebreak:
        try:
            answer = llm_tiebreak(text).strip().lower()
            if answer in ("quick", "agentic"):
                return answer
        except Exception:
            pass
    return "agentic"
