"""V2-1: rule-based retrieval of project memory for one task. No embeddings, no LLM.

Whitelist, not blacklist: an entry reaches a prompt only if it scores above zero against the task.
Signals, strongest first:
  path   an entry's {paths: ...} overlaps the task's scope (prefix match either way)   +3 per match
  dep    the entry names a task this one depends on                                   +2
  words  words shared between the entry and the task's title / spec                   +1 each, max 3
Goal and constraint entries are not ranked: Context Manager always offers them (they are short).
Raw history (event streams, logs, git history) is never a candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .memory import Entry
from .models import Task

CORE_KINDS = ("goal", "constraint")
LIMITS = {"decision": 5, "rejected": 5, "candidate": 5, "change": 5, "knowledge": 5, "architecture": 5}
_STOP = {"with", "that", "this", "from", "into", "when", "will", "should", "must", "have", "file", "files",
         "task", "tests", "test", "code", "make", "only", "each", "than", "then", "also", "they", "them"}
_ASCII_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")


def words(text: str) -> set[str]:
    out = {w.lower() for w in _ASCII_WORD.findall(text)} - _STOP
    for run in _CJK_RUN.findall(text):          # Chinese has no spaces: use character pairs
        out |= {run[i:i + 2] for i in range(len(run) - 1)}
    return out


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip().lstrip("./").rstrip("/")


def path_matches(entry_paths: list[str], scope: list[str]) -> list[str]:
    hits = []
    for e in map(_norm, entry_paths):
        for s in map(_norm, scope):
            if e and s and (e == s or s.startswith(e + "/") or e.startswith(s + "/")):
                hits.append(e)
                break
    return hits


@dataclass
class Scored:
    entry: Entry
    score: int
    reasons: list[str] = field(default_factory=list)


def rank(entries: list[Entry], task: Task) -> list[Scored]:
    """Entries relevant to the task, best first, at most LIMITS[kind] per kind."""
    task_words = words(f"{task.title}\n{task.spec}")
    out: list[Scored] = []
    for e in entries:
        if e.kind in CORE_KINDS:
            continue
        score, why = 0, []
        hits = path_matches(e.paths, task.scope)
        if hits:
            score += 3 * len(hits)
            why.append("path " + ", ".join(hits))
        deps = [d for d in task.depends_on if d and d in e.text]
        if deps:
            score += 2
            why.append("depends " + ", ".join(deps))
        shared = sorted(words(e.text) & task_words)
        if shared:
            score += min(3, len(shared))
            why.append("words " + ", ".join(shared[:5]))
        if score > 0:
            out.append(Scored(e, score, why))
    out.sort(key=lambda s: (-s.score, s.entry.key))
    taken: dict[str, int] = {}
    kept = []
    for s in out:
        n = taken.get(s.entry.kind, 0)
        if n < LIMITS.get(s.entry.kind, 5):
            taken[s.entry.kind] = n + 1
            kept.append(s)
    return kept
