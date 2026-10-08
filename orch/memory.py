"""V2-1: project memory under docs/project/ on the integration branch, and the Memory Writer.

Memory is versioned with the project so every model and every session reads the same thing:

  docs/project/STATE.md      goal, constraints, architecture (written by people) + recent changes (orch)
  docs/project/DECISIONS.md  ADR index (orch, rebuilt from docs/decisions/) + people's own notes
  docs/project/MEMORY.md     long-term knowledge (people) + candidates from agents' PROGRESS.md (orch)

Everything outside `<!-- auto:NAME:start -->` / `<!-- auto:NAME:end -->` belongs to people and is never
rewritten. The Memory Writer is deterministic code: agents only *propose* entries through the
"Rejected approaches" and "Key decisions" sections of PROGRESS.md; a person moves a candidate into
"Long-term knowledge" by editing the file.

An entry is one bullet line. Optional tags at the end, `{paths: src/a.py, tests/}`, tell retrieval
which part of the code it is about.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import workspace as ws

MEMORY_DIR = "docs/project"
FILES = ("STATE.md", "DECISIONS.md", "MEMORY.md")
RECENT_LIMIT = 30

# section heading (as written in the files) -> entry kind used by retrieval
SECTION_KINDS = {
    "目标": "goal", "goal": "goal",
    "约束": "constraint", "constraints": "constraint",
    "架构": "architecture", "architecture": "architecture",
    "最近变更": "change", "recent changes": "change",
    "adr 索引": "decision", "adr index": "decision", "决策": "decision", "decisions": "decision",
    "长期知识": "knowledge", "long-term knowledge": "knowledge",
    "放弃过的方案": "rejected", "rejected approaches": "rejected",
    "候选（待人工确认）": "candidate", "candidates": "candidate",
}

TEMPLATES = {
    "STATE.md": """# 项目状态

<!-- 由人填写：目标、约束、架构。orch 只改下面 auto 标记之间的内容。每条一行，末尾可加 {paths: 目录或文件} -->

## 目标

- （一句话写清这个项目要做成什么）

## 约束

- （例如：只用标准库；必须兼容 Windows）

## 架构

- （主要模块和它们的职责，每条一行，加 {paths: ...}）

## 最近变更

<!-- auto:recent-changes:start -->
<!-- auto:recent-changes:end -->
""",
    "DECISIONS.md": """# 决策记录

## ADR 索引

<!-- auto:adr-index:start -->
<!-- auto:adr-index:end -->

## 决策

- （不需要完整 ADR 的小决定写在这里，每条一行）
""",
    "MEMORY.md": """# 长期记忆

## 长期知识

- （长期有效的项目知识；把下面的候选确认后挪到这里）

## 放弃过的方案

- （试过但不要再试的做法，以及原因）

## 候选（待人工确认）

<!-- agent 在 PROGRESS.md 里写的放弃方案和关键决定，合并时由 orch 追加。确认有用就挪到上面，没用就删掉。 -->
<!-- auto:candidates:start -->
<!-- auto:candidates:end -->
""",
}

_TAGS = re.compile(r"\s*\{paths:\s*([^}]*)\}\s*$")
_PLACEHOLDER = re.compile(r"^[（(].*[）)]$")


@dataclass
class Entry:
    kind: str
    text: str
    file: str                       # repo-relative path, used as the pointer
    paths: list[str] = field(default_factory=list)
    key: str = ""                   # stable id for manifests: FILE:line

    def line(self) -> str:
        return self.text


def parse(text: str, file: str) -> list[Entry]:
    entries: list[Entry] = []
    kind = ""
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s.startswith("## "):
            kind = SECTION_KINDS.get(s[3:].strip().lower(), "")
            continue
        if not kind or not s.startswith("- "):
            continue
        body = s[2:].strip()
        paths: list[str] = []
        m = _TAGS.search(body)
        if m:
            paths = [p.strip().replace("\\", "/") for p in m.group(1).split(",") if p.strip()]
            body = body[: m.start()].rstrip()
        if not body or _PLACEHOLDER.match(body):
            continue  # template placeholder never filled in
        entries.append(Entry(kind=kind, text=body, file=file, paths=paths, key=f"{Path(file).name}:{n}"))
    return entries


# -- reading ---------------------------------------------------------------------

def read_file(orch, name: str) -> str | None:
    """A memory file as committed on the integration branch (None if it does not exist)."""
    try:
        return ws.git(["show", f"{orch.integration_branch}:{MEMORY_DIR}/{name}"], orch.project)
    except ws.GitError:
        return None


def load(orch) -> list[Entry]:
    out: list[Entry] = []
    for name in FILES:
        text = read_file(orch, name)
        if text:
            out += parse(text, f"{MEMORY_DIR}/{name}")
    return out


# -- writing (the Memory Writer) ---------------------------------------------------

def _integ_wt(orch) -> Path:
    return ws.ensure_worktree(orch.project, orch.cfg.worktrees_dir / "_integration",
                              orch.integration_branch, orch.integration_branch)


def replace_block(text: str, name: str, lines: list[str]) -> str:
    start, end = f"<!-- auto:{name}:start -->", f"<!-- auto:{name}:end -->"
    body = "\n".join(lines)
    if start in text and end in text:
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        return f"{head}{start}\n{body + chr(10) if body else ''}{end}{tail}"
    return text.rstrip() + f"\n\n{start}\n{body + chr(10) if body else ''}{end}\n"


def block_lines(text: str, name: str) -> list[str]:
    start, end = f"<!-- auto:{name}:start -->", f"<!-- auto:{name}:end -->"
    if start not in text or end not in text:
        return []
    inner = text.split(start, 1)[1].split(end, 1)[0]
    return [ln for ln in inner.splitlines() if ln.strip()]


def _commit(orch, wt: Path, names: list[str], message: str) -> str | None:
    rels = [f"{MEMORY_DIR}/{n}" for n in names]
    ws.git(["add", "--", *rels], wt)
    if not ws.git(["status", "--porcelain", "--", *rels], wt):
        return None
    ws.git([*ws.ORCH_IDENT, "commit", "--no-verify", "-q", "-m", message], wt)
    return ws.git(["rev-parse", "--short", "HEAD"], wt)


def init(orch) -> tuple[list[str], str | None]:
    """Create the memory files that do not exist yet and commit them. Returns (created, sha)."""
    wt = _integ_wt(orch)
    ws.abort_merge(wt)
    d = wt / MEMORY_DIR
    d.mkdir(parents=True, exist_ok=True)
    created = []
    for name, body in TEMPLATES.items():
        if not (d / name).exists():
            (d / name).write_text(body, encoding="utf-8")
            created.append(name)
    sha = _commit(orch, wt, list(TEMPLATES), "docs: project memory skeleton")
    return created, sha


def commit_human_edits(orch) -> str | None:
    """People edit docs/project/ in the _integration worktree; this commits those edits."""
    wt = _integ_wt(orch)
    return _commit(orch, wt, list(FILES), "docs: project memory edited by hand")


def _section_bullets(progress: str, heading_prefix: str) -> list[str]:
    out, inside = [], False
    for raw in progress.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            inside = s[3:].lower().startswith(heading_prefix)
            continue
        if inside and s.startswith(("- ", "* ")) and len(s) > 3:
            out.append(" ".join(s[2:].split()))
    return out


def on_merge(orch, task, merge_detail: str) -> str | None:
    """After a task is merged: one 'recent change' line + PROGRESS candidates. Caller holds git_lock.

    Does nothing when the project has no docs/project/ yet (`orch memory init` creates it)."""
    wt = _integ_wt(orch)
    d = wt / MEMORY_DIR
    if not (d / "STATE.md").exists():
        return None
    tags = f" {{paths: {', '.join(task.scope)}}}" if task.scope else ""
    sha = merge_detail.split()[-1] if merge_detail else ""
    state = (d / "STATE.md").read_text(encoding="utf-8")
    lines = block_lines(state, "recent-changes")
    lines = [f"- {time.strftime('%Y-%m-%d')} {task.id} {task.title} ({sha}){tags}"] + lines
    (d / "STATE.md").write_text(replace_block(state, "recent-changes", lines[:RECENT_LIMIT]), encoding="utf-8")
    names = ["STATE.md"]

    progress_file = orch.task_dir(task.id) / "PROGRESS.md"
    progress = progress_file.read_text(encoding="utf-8-sig", errors="replace") if progress_file.exists() else ""
    found = ([f"- [{task.id}] 放弃：{b}{tags}" for b in _section_bullets(progress, "rejected")]
             + [f"- [{task.id}] 决定：{b}{tags}" for b in _section_bullets(progress, "key decision")])
    if found and (d / "MEMORY.md").exists():
        mem = (d / "MEMORY.md").read_text(encoding="utf-8")
        old = block_lines(mem, "candidates")
        new = old + [f for f in found if f not in old]
        (d / "MEMORY.md").write_text(replace_block(mem, "candidates", new), encoding="utf-8")
        names.append("MEMORY.md")
    return _commit(orch, wt, names, f"docs: project memory after {task.id}")


_ADR_TITLE = re.compile(r"^# (.+)$", re.M)
_ADR_STATUS = re.compile(r"^- Status: (\S+)", re.M)


def refresh_adr_index(orch, docs_dir: str = "docs/decisions") -> str | None:
    """Rebuild the ADR index in DECISIONS.md from the ADR files on the integration branch."""
    wt = _integ_wt(orch)
    target = wt / MEMORY_DIR / "DECISIONS.md"
    if not target.exists():
        return None
    lines = []
    for f in sorted((wt / docs_dir).glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        title = _ADR_TITLE.search(text)
        status = _ADR_STATUS.search(text)
        rel = f.relative_to(wt).as_posix()
        lines.append(f"- {f.stem.split('-')[0]} {title.group(1).strip() if title else f.stem}"
                     f"（{status.group(1) if status else '?'}） → {rel}")
    target.write_text(replace_block(target.read_text(encoding="utf-8"), "adr-index", lines), encoding="utf-8")
    return _commit(orch, wt, ["DECISIONS.md"], "docs: refresh ADR index")
