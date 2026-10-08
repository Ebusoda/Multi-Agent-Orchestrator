"""M5a: cross-vendor review before merge.

After a task passes its acceptance commands, an agent from a *different* vendor reads the
spec and the diff (read-only) and returns a JSON verdict. Blocker/major issues send the
task back to its author once (configurable); minor ones are recorded but do not block.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import context as ctxm
from . import handoff as ho
from . import workspace as ws
from .adapters import RunContext
from .models import INFRA_FAILURES, RunStatus, Task
from .plan import PlanError, _finish_run
from .i18n import tr

REVIEW_INSTRUCTION = (
    "Read the file .task/REVIEW_PROMPT.md in the current directory and carry out the review it "
    "describes. Do not modify any file."
)
MAX_DIFF_BYTES = 200_000
SEVERITIES = ("blocker", "major", "minor")


def review_prompt(task: Task, author_pool: str, second_round: bool = False) -> str:
    again = (
        "\nThis is a **second review**. Your earlier review is in `.task/REVIEW.md`. Check that each "
        "issue in it was fixed, or that the author explained convincingly in `.task/PROGRESS.md` (Key "
        "decisions) why it should stay. Do not raise new minor points you could have raised before.\n"
        if second_round else ""
    )
    return f"""# Code review assignment from the MAO orchestrator

You are the **Reviewer**. You are in read-only mode: do NOT create, edit or delete files.
Another agent (pool `{author_pool}`) implemented task **{task.id}: {task.title}**, and its
acceptance commands already pass.

Read `.task/TASK.md` (the specification) and `.task/REVIEW_DIFF.patch` (the change against the
integration branch). The author's notes are in `.task/PROGRESS.md`. All three are copied at the end
of this file. Open other files only if the diff does not show enough context; every extra step costs
another model call.
{again}
## What to look for

- Does the change do what the specification asks, including the edge cases it names?
- Bugs, crashes, wrong results, security problems, data loss.
- Tests that pass without really checking the behaviour.
- Changes outside the task's scope, or changes that break other code.

Do not request changes for style preferences or for work outside this task.

Severity: `blocker` = wrong, broken or unsafe; `major` = should be fixed before merging;
`minor` = nice to have.

## Output

Your final message must contain exactly one JSON object in a ```json fenced block:

```json
{{"verdict": "approve", "summary": "one or two sentences",
  "issues": [{{"severity": "major", "file": "path/to/file", "detail": "what is wrong, why, and how to fix it"}}]}}
```

Use `"request_changes"` only when there is at least one blocker or major issue.
"""


def parse_verdict(text: str) -> dict[str, Any]:
    from .plan import _FENCE  # same fenced-block convention as plans

    candidates = list(reversed(_FENCE.findall(text or "")))
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                candidates.append(json.dumps(obj))
            except json.JSONDecodeError:
                pass
    for c in candidates:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            issues = []
            for it in obj.get("issues") or []:
                if not isinstance(it, dict):
                    continue
                sev = str(it.get("severity", "minor")).lower()
                issues.append({
                    "severity": sev if sev in SEVERITIES else "minor",
                    "file": str(it.get("file") or ""),
                    "detail": str(it.get("detail") or "").strip(),
                })
            blocking = [i for i in issues if i["severity"] in ("blocker", "major")]
            # The verdict follows the issues, not the label: "request_changes" with only minor
            # issues is an approval with notes; "approve" with a blocker is not.
            verdict = "request_changes" if blocking else "approve"
            return {"verdict": verdict, "summary": str(obj.get("summary") or "").strip(), "issues": issues}
    raise PlanError("no JSON object with a \"verdict\" field in the reviewer's final message")


def review_markdown(task: Task, pool: str, vendor: str, v: dict[str, Any]) -> str:
    lines = [f"# Review of {task.id} by {pool} ({vendor})", "", f"Verdict: {v['verdict']}", ""]
    if v["summary"]:
        lines += [v["summary"], ""]
    must = [i for i in v["issues"] if i["severity"] in ("blocker", "major")]
    nice = [i for i in v["issues"] if i["severity"] == "minor"]
    if must:
        lines.append("## Issues to fix")
        lines += [f"- [{i['severity']}] {i['file']}: {i['detail']}" for i in must]
        lines.append("")
    if nice:
        lines.append("## Minor (optional)")
        lines += [f"- {i['file']}: {i['detail']}" for i in nice]
    return "\n".join(lines).rstrip() + "\n"


def run_review(orch, task: Task, wt: Path, author_pool: str, author_vendor: str) -> tuple[str, str]:
    """Returns (outcome, note). outcome: approved | changes | skipped.

    A review that cannot be done (no reviewer from another vendor available, unreadable answer)
    is "skipped": it never blocks a task whose acceptance commands pass.
    """
    candidates = orch.cfg.role_pools("review", "review", "pools", task.difficulty)
    integ = orch.integration_branch
    diff = ws.git(["diff", f"{integ}...HEAD"], wt, check=False)
    if not diff.strip():
        return "skipped", "review skipped: no changes against the integration branch"
    if len(diff) > MAX_DIFF_BYTES:
        diff = diff[:MAX_DIFF_BYTES] + "\n\n[diff truncated by the orchestrator; open the files for the rest]\n"
    task_dir = wt / ".task"
    task_dir.mkdir(exist_ok=True)
    second_round = (task_dir / "REVIEW.md").exists() and task.review_rounds > 0
    (task_dir / "REVIEW_DIFF.patch").write_text(diff, encoding="utf-8")
    (task_dir / "REVIEW_PROMPT.md").write_text(review_prompt(task, author_pool, second_round), encoding="utf-8")
    # copies at the end of the prompt: a CLI reviewer needs no tool calls to read them
    ho.inline_files(task_dir / "REVIEW_PROMPT.md",
                    ["TASK.md", "REVIEW_DIFF.patch", "PROGRESS.md"] + (["REVIEW.md"] if second_round else []))

    start = 0
    while True:
        picked = orch.router.pick_from(task, candidates, start, needs_tools=False)
        if picked is None:
            return "skipped", f"review skipped: no reviewer from a vendor other than {author_vendor} is available"
        idx, pool, adapter = picked
        start = idx + 1
        if adapter.vendor == author_vendor:
            continue
        if not orch.acquire(task, pool):  # busy reviewer pool: wait; its breaker opened meanwhile: next
            continue
        try:
            run, result = _review_run(orch, task, wt, pool, adapter, author_pool)
        finally:
            orch.slots.release(pool)
        if ws.dirty(wt):
            # read-only mode should prevent this; never let a reviewer's edits reach the branch
            ws.discard_changes(wt)
            orch.store.log("review_edit_discarded", f"{pool} modified files during review", task_id=task.id)
        if result.status == RunStatus.INTERRUPTED:
            raise KeyboardInterrupt
        if result.status in INFRA_FAILURES or result.status == RunStatus.TIMEOUT:
            orch.breakers.record_failure(pool, result.status, result.resets_at)
            orch.say(tr("[{0}] 审查者 {1} 失败（{2}），换下一个", task.id, pool, result.status.value))
            continue
        orch.breakers.record_success(pool)
        try:
            verdict = parse_verdict(result.summary)
        except PlanError as e:
            return "skipped", f"review by {pool} unreadable ({e}); not blocking"
        md = review_markdown(task, pool, adapter.vendor, verdict)
        (task_dir / "REVIEW.md").write_text(md, encoding="utf-8")
        (orch.task_dir(task.id) / "REVIEW.md").write_text(md, encoding="utf-8")
        (orch.task_dir(task.id) / "runs" / f"{run.id}.review.md").write_text(md, encoding="utf-8")
        n_minor = sum(1 for i in verdict["issues"] if i["severity"] == "minor")
        run.verified = verdict["verdict"] == "approve"
        run.issues_major = len(verdict["issues"]) - n_minor
        run.issues_minor = n_minor
        orch.store.save_run(run)
        if run.verified:
            extra = f", {n_minor} minor note(s) in REVIEW.md" if n_minor else ""
            return "approved", f"reviewed by {pool} ({adapter.vendor}): approved{extra}"
        n_must = len(verdict["issues"]) - n_minor
        return "changes", f"{pool} ({adapter.vendor}) requested changes: {n_must} blocker/major issue(s)"


def _review_run(orch, task: Task, wt: Path, pool: str, adapter, author_pool: str):
    run = orch.store.create_run(task.id, pool, adapter.executor, resumed=False, kind="review")
    events = orch.task_dir(task.id) / "runs" / f"{run.id}.review.jsonl"
    run.events_path = str(events)
    d = wt / ".task"
    manifest = ctxm.measure({n: d / n for n in ("REVIEW_PROMPT.md", "TASK.md", "REVIEW_DIFF.patch", "PROGRESS.md")})
    run.context_tokens = manifest["total"]
    run.context_manifest = ctxm.dumps(manifest)
    orch.store.save_run(run)

    def on_start(pid: int, run=run) -> None:
        run.pid = pid
        orch.store.save_run(run)

    orch.say(tr("[{0}] {1} -> {2} ({3}) 只读审查 {4} 的改动...", task.id, run.id, pool, adapter.vendor, author_pool))
    ctx = RunContext(task=task, worktree=wt, events_path=events,
                     timeout_s=orch._minutes("run_timeout_minutes", 45),
                     model=adapter.model, readonly=True, instruction=REVIEW_INSTRUCTION,
                     on_start=on_start)
    result = adapter.run(ctx)
    _finish_run(orch.store, run, result)
    return run, result
