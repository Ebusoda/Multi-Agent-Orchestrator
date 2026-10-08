"""V2-1: Context Manager. Decides what the model is given for one run, within a token budget.

Principle (core principle 13): Minimum Necessary Context. No history is sent by default; the
model gets what this task needs, and everything else as file pointers it may open itself.

For every run it
  1. measures the files the prompt tells the agent to read (PROMPT, TASK, HANDOFF, REVIEW, VERIFY log),
  2. retrieves project memory relevant to the task (retrieval.py) and always offers goal / constraints,
  3. fits that into the budget of the task's class, compressing deterministically
     (full line -> pointer only -> dropped, lowest score first),
  4. writes .task/CONTEXT.md, and returns a manifest: what was injected, tokens per part, the budget,
     what retrieval found and what compression did. The manifest is stored with the run
     (runs.context_tokens / runs.context_manifest) and shown by `orch context`.

The budget covers what orch injects. The executor's own fixed overhead (system prompt, tool
definitions, files it opens on its own) is not under orch's control; compare with tokens_in.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import memory
from .models import Task
from .retrieval import CORE_KINDS, Scored, rank

BUDGETS = {"small": 1500, "normal": 3000, "large": 6000, "deep": 12000}
CLASS_BY_DIFFICULTY = {"S": "small", "M": "normal", "L": "large"}
_ORDER = ["small", "normal", "large", "deep"]

# what the prompt tells the agent to read in each mode (besides PROMPT.md itself)
MODE_FILES = {
    "fresh": ["TASK.md", "PROGRESS.md"],
    "fix_verify": ["TASK.md", "PROGRESS.md", "VERIFY.log"],
    "takeover": ["TASK.md", "PROGRESS.md", "HANDOFF.md"],
    "address_review": ["TASK.md", "PROGRESS.md", "REVIEW.md"],
    "review_takeover": ["TASK.md", "PROGRESS.md", "HANDOFF.md", "REVIEW.md"],
    "resolve_conflict": ["TASK.md", "PROGRESS.md", "CONFLICTS.md"],
}
KIND_TITLES = {
    "goal": "Project goal", "constraint": "Constraints", "architecture": "Architecture",
    "decision": "Decisions (ADR index)", "knowledge": "Long-term knowledge",
    "rejected": "Rejected approaches (do not retry)", "candidate": "Notes from earlier tasks (unconfirmed)",
    "change": "Recent changes near your scope",
}
_KIND_ORDER = list(KIND_TITLES)


def _given(p: Path) -> str:
    """A prompt file's own text: copies of other files appended after the inline mark are counted
    once, as those files."""
    from .handoff import INLINE_MARK

    text = p.read_text(encoding="utf-8-sig", errors="replace")
    return text.split(INLINE_MARK, 1)[0]


def estimate_tokens(text: str) -> int:
    """Conservative, no tokenizer: 4 ASCII characters ~ 1 token, every other character ~ 1 token."""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return (ascii_n + 3) // 4 + (len(text) - ascii_n)


def budget_class(task: Task, mode: str, cfg: dict) -> str:
    by_type = (cfg.get("class_by_type") or {}).get(task.type)
    cls = by_type or CLASS_BY_DIFFICULTY.get(task.difficulty, "normal")
    if mode in ("takeover", "review_takeover") and _ORDER.index(cls) < 1:
        cls = "normal"  # a takeover carries a HANDOFF note
    return cls


def budget_for(cls: str, cfg: dict) -> int:
    return int((cfg.get("budgets") or {}).get(cls, BUDGETS.get(cls, BUDGETS["normal"])))


def _line(s: Scored | memory.Entry, pointer: bool) -> str:
    e = s.entry if isinstance(s, Scored) else s
    text = e.text if not pointer else (e.text[:40] + "…" if len(e.text) > 40 else e.text)
    return f"- {text} (→ {e.file})" if pointer else f"- {text}"


def render(task: Task, cls: str, budget: int, chosen: list[tuple[Any, bool]]) -> str:
    head = (f"# Project context for {task.id}\n\n"
            f"Selected by the orchestrator for this task only (budget class `{cls}`, {budget} tokens). "
            "Project history is NOT included; open the files named below if you need more.\n")
    if not chosen:
        return head + "\nNo project memory is relevant to this task yet (docs/project/).\n"
    groups: dict[str, list[str]] = {}
    files: set[str] = set()
    for item, pointer in chosen:
        e = item.entry if isinstance(item, Scored) else item
        groups.setdefault(e.kind, []).append(_line(item, pointer))
        files.add(e.file)
    parts = [head]
    for kind in _KIND_ORDER:
        if kind in groups:
            parts.append(f"\n## {KIND_TITLES[kind]}\n" + "\n".join(groups[kind]))
    parts.append("\n## Sources\n" + "\n".join(f"- `{f}`" for f in sorted(files)))
    return "\n".join(parts) + "\n"


def build(orch, task: Task, wt: Path, mode: str) -> dict[str, Any]:
    """Write .task/CONTEXT.md for this run and return its manifest. PROMPT.md must exist already."""
    cfg = orch.cfg.data.get("context", {}) or {}
    d = wt / ".task"
    cls = budget_class(task, mode, cfg)
    budget = budget_for(cls, cfg)

    sections = []
    for name in ["PROMPT.md", *MODE_FILES.get(mode, ["TASK.md"])]:
        p = d / name
        if p.exists():
            sections.append({"name": name, "kind": "mandatory", "tokens": estimate_tokens(_given(p))})
    mandatory = sum(s["tokens"] for s in sections)

    entries = memory.load(orch) if cfg.get("enabled", True) else []
    core = [e for e in entries if e.kind in CORE_KINDS]
    ranked = rank(entries, task)
    # candidates in priority order: core first (always relevant), then by score
    items: list[dict[str, Any]] = (
        [{"obj": e, "key": e.key, "kind": e.kind, "score": None, "reasons": ["core"], "mode": "full"} for e in core]
        + [{"obj": s, "key": s.entry.key, "kind": s.entry.kind, "score": s.score, "reasons": s.reasons,
            "mode": "full"} for s in ranked]
    )
    steps: list[dict[str, str]] = []

    def text_now() -> str:
        return render(task, cls, budget,
                      [(i["obj"], i["mode"] == "pointer") for i in items if i["mode"] != "dropped"])

    room = budget - mandatory
    # compress lowest priority first: full -> pointer, then pointer -> dropped
    for target in ("pointer", "dropped"):
        for it in reversed(items):
            if estimate_tokens(text_now()) <= room:
                break
            if it["mode"] != target and (target == "pointer") == (it["mode"] == "full"):
                it["mode"] = target
                steps.append({"step": target, "key": it["key"], "kind": it["kind"]})
    text = text_now()
    (d / "CONTEXT.md").write_text(text, encoding="utf-8")
    ctx_tokens = estimate_tokens(text)
    sections.append({"name": "CONTEXT.md", "kind": "retrieved", "tokens": ctx_tokens})
    for it in items:
        it["tokens"] = estimate_tokens(_line(it["obj"], it["mode"] == "pointer")) if it["mode"] != "dropped" else 0
    total = mandatory + ctx_tokens
    return {
        "mode": mode, "class": cls, "budget": budget, "total": total, "mandatory": mandatory,
        "over_budget": total > budget,
        "sections": sections,
        "memory_entries": len(entries),
        "retrieved": [{k: it[k] for k in ("key", "kind", "score", "reasons", "mode", "tokens")} for it in items],
        "compression": steps,
        "raw_history_injected": False,
    }


def measure(files: dict[str, Path], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Manifest for runs whose prompt orch builds elsewhere (review): measured, no budget enforced."""
    sections = []
    for name, p in files.items():
        if p.exists():
            sections.append({"name": name, "kind": "mandatory", "tokens": estimate_tokens(_given(p))})
    total = sum(s["tokens"] for s in sections)
    return {"mode": "review", "class": "observe", "budget": None, "total": total, "mandatory": total,
            "over_budget": False, "sections": sections, "memory_entries": 0, "retrieved": [],
            "compression": [], "raw_history_injected": False, **(extra or {})}


def summary_line(m: dict[str, Any]) -> str:
    """One line for the console: 'normal 1,234/3,000 tokens, 4 retrieved (1 pointer), 0 dropped'."""
    full = sum(1 for r in m["retrieved"] if r["mode"] == "full")
    ptr = sum(1 for r in m["retrieved"] if r["mode"] == "pointer")
    drop = sum(1 for r in m["retrieved"] if r["mode"] == "dropped")
    budget = f"/{m['budget']:,}" if m.get("budget") else ""
    over = " 超预算" if m.get("over_budget") else ""
    return (f"上下文 {m['class']} {m['total']:,}{budget} tokens{over}；记忆 {m['memory_entries']} 条，"
            f"注入 {full} 条全文、{ptr} 条指针，丢弃 {drop} 条")


def dumps(m: dict[str, Any]) -> str:
    return json.dumps(m, ensure_ascii=False)
