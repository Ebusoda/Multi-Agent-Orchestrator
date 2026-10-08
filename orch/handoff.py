"""Files the agent reads (.task/*) and the handoff note passed between executors.

Three kinds of state are kept apart:
  control state  -> SQLite (orchestrator only)
  work state     -> the task's worktree + branch + checkpoint commits
  cognitive state-> .task/PROGRESS.md (written by the agent) + .task/HANDOFF.md (by orch)
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .models import Run, Task
from .workspace import diff_stat

TASK_DIR = ".task"

ROLE_BY_TYPE = {
    "code_change": "Coding Agent",
    "research": "Research Agent",
    "review": "Reviewer Agent",
}

PROGRESS_TEMPLATE = """\
# Progress for {id}
## Done
## In progress
## Next steps
## Rejected approaches (and why)
## Key decisions
## How to verify
"""


def task_dir(wt: Path) -> Path:
    d = wt / TASK_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- `{i}`" for i in items) if items else f"- {empty}"


def write_task_files(wt: Path, task: Task, mode: str) -> Path:
    """Write TASK.md / TASK.json / PROMPT.md.

    mode: fresh | takeover | fix_verify | address_review (resumed author) | review_takeover
    """
    d = task_dir(wt)
    (d / "TASK.json").write_text(
        json.dumps(
            {k: getattr(task, k) for k in ("id", "title", "spec", "type", "difficulty", "risk", "scope", "verify")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    task_md = (
        f"# {task.id}: {task.title}\n\n"
        f"Type: {task.type} | Difficulty: {task.difficulty} | Risk: {task.risk}\n\n"
        f"## Specification\n\n{task.spec or '(see title)'}\n\n"
        f"## Scope (files you should change)\n\n{_bullets(task.scope, 'not restricted')}\n\n"
        f"## Acceptance: these commands must all exit with code 0\n\n{_bullets(task.verify, 'none defined')}\n"
    )
    (d / "TASK.md").write_text(task_md, encoding="utf-8")
    progress = d / "PROGRESS.md"
    if not progress.exists():
        progress.write_text(PROGRESS_TEMPLATE.format(id=task.id), encoding="utf-8")

    role = ROLE_BY_TYPE.get(task.type, "Agent")
    if mode == "fix_verify":
        opening = (
            "## Your previous attempt did not pass acceptance\n\n"
            "The orchestrator ran the acceptance commands and they FAILED. The output is in "
            "`.task/VERIFY.log`. Read it, fix the cause, run the commands yourself until they pass, "
            "and update `.task/PROGRESS.md`.\n"
        )
    elif mode == "address_review":
        opening = (
            "## A reviewer asked for changes\n\n"
            "Your change passed the acceptance commands, but a reviewer from another company read it "
            "and found problems. Read `.task/REVIEW.md` and fix every item under \"Issues to fix\" "
            "(the \"Minor\" ones are optional). If you think an item is wrong, do not ignore it: explain "
            "why in `.task/PROGRESS.md` under Key decisions, the next reviewer will read that. Run the "
            "acceptance commands again before you stop.\n"
        )
    elif mode == "review_takeover":
        opening = (
            "## You are taking over, and a reviewer asked for changes\n\n"
            "Another agent implemented this task and it passed the acceptance commands, but a reviewer "
            "found problems. Read `.task/HANDOFF.md`, then `.task/REVIEW.md`, and fix every item under "
            "\"Issues to fix\". If you think an item is wrong, explain why in `.task/PROGRESS.md` under "
            "Key decisions. Run the acceptance commands again before you stop.\n"
        )
    elif mode == "resolve_conflict":
        opening = (
            "## Resolve a merge conflict\n\n"
            "Your work on this task passed acceptance, but the integration branch moved on meanwhile. The "
            "orchestrator has started merging the integration branch into this worktree and git stopped on "
            "conflicts. `.task/CONFLICTS.md` lists the files. In each one, edit the conflicted regions "
            "(between `<<<<<<<` and `>>>>>>>`) so that BOTH sides' intent is kept, and remove every conflict "
            "marker. Do not run any git command; the orchestrator finishes the merge. Run the acceptance "
            "commands before you stop.\n"
        )
    elif mode == "takeover":
        opening = (
            "## You are taking over from another agent\n\n"
            "Read `.task/HANDOFF.md` first. Do not trust its claims blindly: run the acceptance "
            "commands to see the real current state of the code, then continue from there. "
            "The previous agent's changes are already in this directory.\n"
        )
    else:
        opening = "## Start\n\nRead `.task/TASK.md`, then do the task.\n"
    opening += (
        "\nThe files named here (`.task/TASK.md`, `.task/CONTEXT.md` with the project context the "
        "orchestrator selected, and any handoff or review notes) are copied in full at the end of this "
        "file. You do not need to open them.\n"
    )

    prompt = f"""# Assignment from the MAO orchestrator

You are the **{role}** for task **{task.id}: {task.title}**.
Your working directory is a dedicated git worktree for this task. Only modify files inside it.

{opening}
## Rules

1. Do NOT run `git commit`, `git checkout`, `git switch`, `git reset`, `git stash` or change branches.
   The orchestrator commits checkpoints for you.
2. Keep `.task/PROGRESS.md` up to date (Done / In progress / Next steps / Rejected approaches /
   Key decisions / How to verify). Update it before you stop. Another agent may continue your work
   from this file alone.
3. Before you finish, run every acceptance command listed in `.task/TASK.md` yourself. If a command
   cannot run in your environment (sandbox restrictions, interpreter not found), do not spend time
   working around it: write that in `.task/PROGRESS.md` and finish. The orchestrator runs acceptance
   itself after you stop.
4. If you are truly blocked (missing information, contradictory requirements), write
   `BLOCKED: <reason>` as the very first line of `.task/PROGRESS.md` and stop.
5. Do not edit files under `.task/` except `PROGRESS.md`.
6. Work in few steps: every step re-sends the whole conversation to the model. Read several files
   with one command, do not re-read what is already in this prompt, and do not run `git status` or
   `git diff` just to look over your own work at the end.
7. Finish with a short summary of what you changed.
"""
    path = d / "PROMPT.md"
    path.write_text(prompt, encoding="utf-8")
    return path


INLINE_MARK = "<!-- orch:inlined -->"
VERIFY_TAIL_LINES = 80


def inline_files(prompt_path: Path, names: list[str]) -> list[str]:
    """Append the named .task/ files to the prompt, so the agent needs no round trip to read them.

    Each file read by a tool call costs one more model call, and every call re-sends the whole
    conversation. Returns the names actually included. VERIFY.log is cut to its tail."""
    d = prompt_path.parent
    if INLINE_MARK in prompt_path.read_text(encoding="utf-8", errors="replace"):
        return []  # already done (a second reviewer reuses the same prompt)
    parts, done = [], []
    for name in dict.fromkeys(names):
        p = d / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8-sig", errors="replace").rstrip()
        if name == "VERIFY.log":
            lines = text.splitlines()
            if len(lines) > VERIFY_TAIL_LINES:
                text = f"[first {len(lines) - VERIFY_TAIL_LINES} lines left out; full log in .task/VERIFY.log]\n" + \
                    "\n".join(lines[-VERIFY_TAIL_LINES:])
        fence = "````" if "```" in text else "```"
        parts.append(f"\n## .task/{name}\n\n{fence}\n{text}\n{fence}\n")
        done.append(name)
    if parts:
        with prompt_path.open("a", encoding="utf-8") as f:
            f.write(f"\n---\n{INLINE_MARK}\n# Included files (copies; you do not need to open them)\n"
                    + "".join(parts))
    return done


def read_progress(wt: Path) -> str:
    p = wt / TASK_DIR / "PROGRESS.md"
    # utf-8-sig: PowerShell's Set-Content writes a BOM, which would hide a leading "BLOCKED:"
    return p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""


def blocked_reason(wt: Path) -> str | None:
    text = read_progress(wt).lstrip()
    if text.upper().startswith("BLOCKED:"):
        return text.splitlines()[0][len("BLOCKED:"):].strip() or "no reason given"
    return None


_PS_WRAPPER = re.compile(r'^"?[A-Za-z]:\\\\?[^"]*powershell\.exe"?\s+-Command\s+', re.I)


def _short(value, limit: int = 160) -> str:
    if isinstance(value, dict):
        for key in ("file_path", "filePath", "path", "command", "pattern"):
            if value.get(key):
                value = value[key]
                break
        else:
            value = json.dumps(value, ensure_ascii=False)
    text = " ".join(str(value).split())
    text = _PS_WRAPPER.sub("", text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def recent_actions(events_path: str | Path, limit: int = 12) -> list[str]:
    """Tool calls / commands from a run's raw event stream (Claude Code, Codex, opencode)."""
    acts: list[str] = []
    try:
        lines = Path(events_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return acts
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "assistant":  # Claude Code
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    acts.append(f"{block.get('name')}: {_short(block.get('input') or {})}")
        elif t == "item.completed":  # Codex
            item = ev.get("item") or {}
            if item.get("type") == "command_execution":
                acts.append(f"command (exit {item.get('exit_code')}): {_short(item.get('command', ''))}")
            elif item.get("type") == "file_change":
                paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
                acts.append("file change: " + ", ".join(p for p in paths if p))
        elif t == "tool_use":  # opencode
            part = ev.get("part") or {}
            state = part.get("state") or {}
            acts.append(f"{part.get('tool')} ({state.get('status')}): {_short(state.get('input') or {})}")
    return acts[-limit:]


def _is_template(progress: str) -> bool:
    return all(not line.strip() or line.lstrip().startswith("#") for line in progress.splitlines())


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def build_handoff(task: Task, prev: Run, wt: Path, base_ref: str, verify_log: str, reason: str) -> str:
    """Deterministic handoff note (no LLM): facts the orchestrator knows + the agent's own notes."""
    # (no backslashes inside f-string expressions: keeps Python 3.11 compatibility)
    progress = read_progress(wt).strip()
    if _is_template(progress):
        progress = "(the previous agent left no notes; see its last actions below)"
    actions = recent_actions(prev.events_path) if prev.events_path else []
    actions_text = "\n".join(f"- {a}" for a in actions) or "(no tool calls recorded)"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(prev.ended_at or time.time()))
    last_words = "\n".join(x for x in (prev.summary or "", prev.error or "") if x)
    last_words = _tail(last_words, 40) or "(none)"
    verify_tail = _tail(verify_log, 60) if verify_log else "(acceptance was not run for that attempt)"
    changes = diff_stat(wt, base_ref)
    return f"""# HANDOFF for {task.id} (generated by the orchestrator)

## Why you are taking over
{reason}

## Previous run
- Run: {prev.id} | pool: {prev.pool} | executor: {prev.executor}
- Result: {prev.status.value} | exit code: {prev.exit_code} | ended: {when}
- Passed acceptance: {prev.verified}

## Previous agent's own notes (.task/PROGRESS.md)
{progress}

## What the previous agent did last (from its event log, oldest first)
{actions_text}

## Code changes so far (relative to the integration branch)
```
{changes}
```

## Last acceptance output (tail)
```
{verify_tail}
```

## Previous agent's last message / error (tail)
```
{last_words}
```

## What to do now
1. Run the acceptance commands from `.task/TASK.md` to see the real state.
2. Continue the task; avoid the approaches listed as rejected above.
3. Keep `.task/PROGRESS.md` updated as you go.
"""


def write_handoff(wt: Path, text: str) -> None:
    (task_dir(wt) / "HANDOFF.md").write_text(text, encoding="utf-8")


def write_verify_log(wt: Path, log: str) -> None:
    (task_dir(wt) / "VERIFY.log").write_text(log, encoding="utf-8")


def archive(wt: Path, dest: Path, run_id: str) -> None:
    """Copy the agent-facing files into .agents/tasks/<id>/ so history survives worktree removal."""
    dest.mkdir(parents=True, exist_ok=True)
    src = wt / TASK_DIR
    for name in ("TASK.md", "HANDOFF.md", "PROMPT.md", "CONTEXT.md"):
        if (src / name).exists():
            shutil.copy2(src / name, dest / name)
    if (src / "PROGRESS.md").exists():
        runs = dest / "runs"
        runs.mkdir(exist_ok=True)
        shutil.copy2(src / "PROGRESS.md", runs / f"{run_id}.progress.md")
        shutil.copy2(src / "PROGRESS.md", dest / "PROGRESS.md")
    if (src / "CONTEXT.md").exists():
        (dest / "runs").mkdir(exist_ok=True)
        shutil.copy2(src / "CONTEXT.md", dest / "runs" / f"{run_id}.context.md")
