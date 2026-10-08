"""M4: the Manager. One read-only agent run turns a goal into a task plan; a human approves it.

The LLM only *proposes* (a JSON plan). Everything after that - validation, task creation,
scheduling, merging - is deterministic code.
"""
from __future__ import annotations

import json
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import workspace as ws
from .adapters import RunContext
from .models import INFRA_FAILURES, RunStatus, Task

DIFFICULTIES = ("S", "M", "L")
RISKS = ("low", "normal", "high", "critical")
MAX_TASKS = 12

PLAN_EXAMPLE = {
    "summary": "One or two sentences: what will be built and in which order.",
    "tasks": [
        {
            "key": "t1",
            "title": "Short imperative title",
            "spec": "What to change and how to know it is right. Mention files, functions, edge cases.",
            "type": "code_change",
            "difficulty": "S",
            "risk": "normal",
            "scope": ["src/module.py", "tests/test_module.py"],
            "verify": ["<command that exits 0 only when this task is done>"],
            "depends_on": [],
        }
    ],
}


class PlanError(ValueError):
    pass


@dataclass
class Plan:
    summary: str
    tasks: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


# -- prompt ---------------------------------------------------------------------

def planner_prompt(goal: str, context: str = "") -> str:
    shell = "cmd.exe (Windows)" if sys.platform == "win32" else "/bin/sh"
    example = json.dumps(PLAN_EXAMPLE, ensure_ascii=False, indent=2)
    extra = f"\n## Extra context from the user\n\n{context.strip()}\n" if context.strip() else ""
    return f"""# Planning assignment from the MAO orchestrator

You are the **Manager**. Your job is to split a goal into tasks that other coding agents will
carry out one at a time. You are in **read-only mode**: do NOT create, edit or delete any file.
Read whatever you need in the current directory (the project's integration branch).

## Goal

{goal.strip()}
{extra}
## Environment

- OS: {platform.system()} {platform.release()}
- Acceptance commands run from the repository root through {shell}.
- Python available to acceptance commands: "{sys.executable}" (quote the path in commands).

## Rules for the plan

1. 1 to 8 tasks. Each task must be finishable by one agent in one session (well under an hour).
2. Every task needs `verify`: commands that exit 0 only when that task is done. Prefer the
   project's existing tests. If a task needs new tests, writing them is part of that task.
3. `scope` lists the files or directories the task should change. Tasks that change the same
   files must be ordered with `depends_on` (list the keys of tasks that must be merged first).
4. `difficulty`: S = small and mechanical, M = normal, L = hard or design-heavy.
   `risk`: low / normal / high / critical (critical = data loss, security, money).
5. Do not plan work that is already done. Do not plan a separate "run the tests" task.

## Output

Your final message must contain exactly one JSON object in a ```json fenced block, shaped like:

```json
{example}
```
"""


def fix_prompt(error: str) -> str:
    return (
        "The orchestrator could not accept your plan:\n\n"
        f"{error}\n\n"
        "Reply with the corrected plan only: one JSON object in a ```json fenced block."
    )


# -- parsing & validation -----------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S | re.I)


def extract_json(text: str) -> dict[str, Any]:
    """Last JSON object in the text: fenced ```json blocks first, then any {...}."""
    for block in reversed(_FENCE.findall(text or "")):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tasks" in obj:
            found = obj
    if found is None:
        raise PlanError("no JSON object with a \"tasks\" list was found in the final message")
    return found


def _str_list(value: Any, name: str, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"task {key}: `{name}` must be a list of strings")
    return [v for v in value if v.strip()]


def validate(raw: dict[str, Any]) -> Plan:
    tasks_in = raw.get("tasks")
    if not isinstance(tasks_in, list) or not tasks_in:
        raise PlanError("`tasks` must be a non-empty list")
    if len(tasks_in) > MAX_TASKS:
        raise PlanError(f"{len(tasks_in)} tasks is too many (max {MAX_TASKS}); merge some of them")
    tasks: list[dict[str, Any]] = []
    keys: set[str] = set()
    for i, t in enumerate(tasks_in, 1):
        if not isinstance(t, dict):
            raise PlanError(f"task #{i} is not an object")
        key = str(t.get("key") or f"t{i}").strip()
        if key in keys:
            raise PlanError(f"duplicate task key {key!r}")
        keys.add(key)
        title = str(t.get("title") or "").strip()
        if not title:
            raise PlanError(f"task {key}: missing title")
        difficulty = str(t.get("difficulty") or "M").strip().upper()[:1]
        if difficulty not in DIFFICULTIES:
            raise PlanError(f"task {key}: difficulty must be one of {DIFFICULTIES}")
        risk = str(t.get("risk") or "normal").strip().lower()
        if risk not in RISKS:
            raise PlanError(f"task {key}: risk must be one of {RISKS}")
        tasks.append({
            "key": key,
            "title": title,
            "spec": str(t.get("spec") or "").strip(),
            "type": str(t.get("type") or "code_change").strip() or "code_change",
            "difficulty": difficulty,
            "risk": risk,
            "scope": _str_list(t.get("scope"), "scope", key),
            "verify": _str_list(t.get("verify"), "verify", key),
            "depends_on": _str_list(t.get("depends_on"), "depends_on", key),
        })
    for t in tasks:
        for d in t["depends_on"]:
            if d == t["key"]:
                raise PlanError(f"task {d} depends on itself")
            if d not in keys:
                raise PlanError(f"task {t['key']} depends on unknown key {d!r}")
    ordered = topo_order(tasks)  # raises on cycles
    plan = Plan(summary=str(raw.get("summary") or "").strip(), tasks=ordered)
    plan.warnings = lint(plan)
    return plan


def topo_order(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {t["key"]: t for t in tasks}
    done: list[str] = []
    state: dict[str, int] = {}

    def visit(k: str, path: list[str]) -> None:
        if state.get(k) == 2:
            return
        if state.get(k) == 1:
            raise PlanError("dependency cycle: " + " -> ".join(path + [k]))
        state[k] = 1
        for d in by_key[k]["depends_on"]:
            visit(d, path + [k])
        state[k] = 2
        done.append(k)

    for t in tasks:  # keep the planner's order where dependencies allow
        visit(t["key"], [])
    return [by_key[k] for k in done]


def _ancestors(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    by_key = {t["key"]: t for t in tasks}
    memo: dict[str, set[str]] = {}

    def anc(k: str) -> set[str]:
        if k not in memo:
            memo[k] = set()
            for d in by_key[k]["depends_on"]:
                memo[k] |= {d} | anc(d)
        return memo[k]

    for t in tasks:
        anc(t["key"])
    return memo


def _overlaps(a: str, b: str) -> bool:
    a, b = a.replace("\\", "/").rstrip("/"), b.replace("\\", "/").rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def lint(plan: Plan) -> list[str]:
    warnings: list[str] = []
    anc = _ancestors(plan.tasks)
    for t in plan.tasks:
        if not t["verify"]:
            warnings.append(f"{t['key']}: no verify command - completion will rest on the agent's word")
    for i, a in enumerate(plan.tasks):
        for b in plan.tasks[i + 1:]:
            if a["key"] in anc[b["key"]] or b["key"] in anc[a["key"]]:
                continue
            shared = sorted({x for x in a["scope"] for y in b["scope"] if _overlaps(x, y)})
            if shared:
                warnings.append(
                    f"{a['key']} and {b['key']} both touch {', '.join(shared)} without depends_on "
                    "(they will run one after another, but a merge conflict is possible)"
                )
    return warnings


def render(plan_id: str, goal: str, plan: Plan, status: str) -> str:
    lines = [f"# {plan_id}  [{status}]", "", f"目标: {goal.strip()}", ""]
    if plan.summary:
        lines += [f"概要: {plan.summary}", ""]
    for n, t in enumerate(plan.tasks, 1):
        deps = f"  依赖: {', '.join(t['depends_on'])}" if t["depends_on"] else ""
        lines.append(f"{n}. [{t['key']}] {t['title']}  ({t['type']}/{t['difficulty']}, risk={t['risk']}){deps}")
        if t["spec"]:
            for s in t["spec"].splitlines():
                lines.append(f"     {s}")
        if t["scope"]:
            lines.append(f"     范围: {', '.join(t['scope'])}")
        for v in t["verify"]:
            lines.append(f"     验收: {v}")
        lines.append("")
    if plan.warnings:
        lines.append("提醒:")
        lines += [f"  - {w}" for w in plan.warnings]
    return "\n".join(lines).rstrip() + "\n"


# -- running the planner -------------------------------------------------------------

def run_planner(orch, goal: str, context: str = "", pool: str | None = None) -> tuple[str, Plan]:
    """Run a read-only planning session and store the resulting draft plan."""
    store, cfg = orch.store, orch.cfg
    plan_id = store.new_plan(goal)
    pseudo = Task(id=plan_id, title="plan", type="plan", difficulty="M")
    if pool:
        if pool not in cfg.pools:
            raise PlanError(f"pool {pool!r} is not defined")
        pseudo_route = [pool]
    else:
        pseudo_route = orch.router.route(pseudo)
    if not pseudo_route:
        raise PlanError("no routing rule for planning; add `plan = [...]` under [routing]")

    integ = orch.integration_branch
    wt = ws.ensure_detached_worktree(orch.project, cfg.worktrees_dir / "_planning", integ)
    task_dir = wt / ".task"
    task_dir.mkdir(exist_ok=True)
    plan_dir = cfg.agents_dir / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)

    last_error = ""
    start = 0
    while True:
        picked = orch.router.pick_from(pseudo, pseudo_route, start)
        if picked is None:
            break
        idx, pool_name, adapter = picked
        start = idx + 1  # a pool that fails here is not retried for this plan
        (task_dir / "PROMPT.md").write_text(planner_prompt(goal, context), encoding="utf-8")
        run = store.create_run(plan_id, pool_name, adapter.executor, resumed=False, kind="plan")
        events = plan_dir / plan_id / f"{run.id}.jsonl"
        run.events_path = str(events)
        orch.say(f"[{plan_id}] {run.id} -> {pool_name} ({adapter.executor}), 只读规划中...")
        ctx = RunContext(task=pseudo, worktree=wt, events_path=events,
                         timeout_s=orch._minutes("run_timeout_minutes", 45),
                         model=adapter.model, readonly=True)
        result = adapter.run(ctx)
        _finish_run(store, run, result)
        if result.status in INFRA_FAILURES or result.status == RunStatus.TIMEOUT:
            orch.breakers.record_failure(pool_name, result.status, result.resets_at)
            last_error = f"{pool_name}: {result.status.value} {result.error[-300:]}"
            orch.say(f"[{plan_id}] {pool_name} 失败（{result.status.value}），换下一个")
            continue
        orch.breakers.record_success(pool_name)
        try:
            plan = validate(extract_json(result.summary))
        except PlanError as e:
            last_error = str(e)
            if not (result.session_id and adapter.supports_resume()):
                break
            orch.say(f"[{plan_id}] 计划不合格（{e}），让它改一次")
            (task_dir / "PROMPT.md").write_text(fix_prompt(str(e)), encoding="utf-8")
            run2 = store.create_run(plan_id, pool_name, adapter.executor, resumed=True, kind="plan")
            run2.events_path = str(plan_dir / plan_id / f"{run2.id}.jsonl")
            ctx2 = RunContext(task=pseudo, worktree=wt, events_path=Path(run2.events_path),
                              timeout_s=ctx.timeout_s, model=adapter.model,
                              resume_session=result.session_id, readonly=True)
            result2 = adapter.run(ctx2)
            _finish_run(store, run2, result2)
            try:
                plan = validate(extract_json(result2.summary))
            except PlanError as e2:
                last_error = str(e2)
                break
        store.save_plan(plan_id, "draft", pool_name, plan)
        (plan_dir / f"{plan_id}.md").write_text(render(plan_id, goal, plan, "draft"), encoding="utf-8")
        return plan_id, plan
    store.save_plan(plan_id, "failed", "", None, error=last_error)
    raise PlanError(f"{plan_id}: planning failed - {last_error or 'no available pool'}")


def _finish_run(store, run, result) -> None:
    run.status = result.status
    run.session_id = result.session_id
    run.exit_code = result.exit_code
    run.est_cost_usd = result.est_cost_usd
    run.tokens_in, run.tokens_out = result.tokens_in, result.tokens_out
    run.tokens_cached, run.model_calls = result.tokens_cached, result.model_calls
    run.quota = json.dumps(result.quota) if result.quota else ""
    run.summary = (result.summary or "")[-8000:]
    run.error = (result.error or "")[-4000:]
    run.ended_at = time.time()
    store.save_run(run)


def approve(orch, plan_id: str) -> list[Task]:
    row = orch.store.get_plan(plan_id)
    if row is None:
        raise PlanError(f"no plan {plan_id}")
    if row["status"] != "draft":
        raise PlanError(f"{plan_id} is {row['status']}; only a draft plan can be approved")
    data = json.loads(row["plan_json"])
    plan = validate(data)
    key_to_id: dict[str, str] = {}
    created: list[Task] = []
    max_runs = int(orch.cfg.esc.get("max_runs_per_task", 4))
    for t in plan.tasks:  # already in dependency order
        task = orch.store.add_task(Task(
            id="", title=t["title"], spec=t["spec"], type=t["type"], difficulty=t["difficulty"],
            risk=t["risk"], scope=t["scope"], verify=t["verify"],
            depends_on=[key_to_id[d] for d in t["depends_on"]], max_runs=max_runs,
            note=f"from {plan_id}/{t['key']}",
        ))
        key_to_id[t["key"]] = task.id
        created.append(task)
    orch.store.set_plan_status(plan_id, "approved", json.dumps(key_to_id))
    md = orch.cfg.agents_dir / "plans" / f"{plan_id}.md"
    md.write_text(render(plan_id, row["goal"], plan, "approved"), encoding="utf-8")
    return created
