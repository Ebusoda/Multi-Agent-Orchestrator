"""V2-2: execution records -> `orch report`.

Read-only over state.db (the `run_records` view plus tasks / plans / log). Measures what the
project cares about: cost per *successful* task, not per call.

Counting rules
- A task is any T- id with at least one work run. Success = verified or merged.
- First pass = its first work run passed acceptance and no review sent it back.
- A task is credited to the pool of its last work run; attempts per pool are listed too.
- Task cost = all its work + review runs, plus an equal share of the plan that created it.
- API pools report real dollars; Claude's dollars are an estimate ("≈"); Codex reports tokens only.
- Plan (P-) and decide (D-) runs are listed on their own.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import Run, TaskStatus
from .store import Store

ESTIMATED_COST_EXECUTORS = {"claude_code"}  # dollars come from the CLI's own estimate
_SUCCESS = {TaskStatus.VERIFIED, TaskStatus.MERGED}
_ISSUE_LINE = re.compile(r"^- \[(blocker|major)\]", re.M)


def parse_since(text: str | None, now: float | None = None) -> float | None:
    """'7d' (last 7 days) or 'YYYY-MM-DD' (local midnight) -> epoch seconds."""
    if not text:
        return None
    text = text.strip()
    m = re.fullmatch(r"(\d+)d", text)
    if m:
        return (now or time.time()) - int(m.group(1)) * 86400
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d"))
    except ValueError:
        raise ValueError(f"--since 需要 7d 或 2026-10-01 这样的格式，收到 {text!r}") from None


def _secs(r: Run) -> float:
    return max(0.0, (r.ended_at or r.started_at) - r.started_at)


def review_issues(r: Run, task_dir: Path) -> int:
    """Blocker/major issues a review raised. Older runs have no column; read their REVIEW.md copy."""
    if r.issues_major is not None:
        return r.issues_major
    md = task_dir / "runs" / f"{r.id}.review.md"
    try:
        return len(_ISSUE_LINE.findall(md.read_text(encoding="utf-8")))
    except OSError:
        return 0 if r.verified else 1


class _Sum:
    def __init__(self, estimated_pools: set[str]) -> None:
        self.estimated_pools = estimated_pools
        self.estimated = False
        self.runs = 0
        self.usd = 0.0
        self.has_usd = False
        self.tin = 0
        self.tout = 0
        self.secs = 0.0
        self.cached = 0
        self.calls = 0
        self.calls_runs = 0
        self.ctx = 0       # tokens orch injected (Context Manager), over runs that recorded it
        self.ctx_runs = 0
        self.ctx_tin = 0   # the executor's own tokens_in for those same runs

    def add(self, r: Run, share: float = 1.0) -> None:
        self.runs += 1
        if r.est_cost_usd is not None:
            self.usd += r.est_cost_usd * share
            self.has_usd = True
            self.estimated |= r.pool in self.estimated_pools
        self.tin += int((r.tokens_in or 0) * share)
        self.tout += int((r.tokens_out or 0) * share)
        self.secs += _secs(r) * share
        self.cached += int((r.tokens_cached or 0) * share)
        if r.model_calls and share == 1.0:
            self.calls += r.model_calls
            self.calls_runs += 1
        if r.context_tokens is not None and share == 1.0:
            self.ctx += r.context_tokens
            self.ctx_runs += 1
            self.ctx_tin += r.tokens_in or 0

    def as_dict(self) -> dict[str, Any]:
        return {"runs": self.runs, "usd": round(self.usd, 6) if self.has_usd else None,
                "estimated_usd": self.estimated,
                "tokens_in": self.tin, "tokens_out": self.tout, "seconds": round(self.secs, 1),
                "tokens_cached": self.cached,
                "uncached_avg": (self.tin - self.cached) // self.runs if self.runs else None,
                "calls_avg": round(self.calls / self.calls_runs, 1) if self.calls_runs else None,
                "context_avg": self.ctx // self.ctx_runs if self.ctx_runs else None,
                "tokens_in_avg_with_context": self.ctx_tin // self.ctx_runs if self.ctx_runs else None}


def build_report(store: Store, agents_dir: Path, executors: dict[str, str],
                 since: float | None = None, task_type: str | None = None) -> dict[str, Any]:
    """executors: pool -> executor name (to mark estimated dollars)."""
    est = {pool for pool, ex in executors.items() if ex in ESTIMATED_COST_EXECUTORS}
    runs = [store._run_from_row(r) for r in store.db.execute("SELECT * FROM run_records ORDER BY id")]
    if since is not None:
        runs = [r for r in runs if r.started_at >= since]
    by_owner: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        by_owner[r.task_id].append(r)

    # plan cost shared equally by the tasks the plan created
    plan_share: dict[str, list[tuple[Run, float]]] = defaultdict(list)
    for p in store.list_plans():
        ids = list(json.loads(p["task_map"] or "{}").values())
        for r in by_owner.get(p["id"], []):
            for tid in ids:
                plan_share[tid].append((r, 1 / len(ids)))

    escalations: dict[str, int] = defaultdict(int)
    for row in store.db.execute("SELECT task_id FROM log WHERE kind='escalate'"):
        escalations[row["task_id"]] += 1

    tasks = []
    for t in store.list_tasks():
        own = by_owner.get(t.id, [])
        work = [r for r in own if r.kind == "work"]
        if not work or (task_type and t.type != task_type):
            continue
        reviews = [r for r in own if r.kind == "review"]
        cost = _Sum(est)
        for r in own:
            cost.add(r)
        for r, share in plan_share.get(t.id, []):
            cost.add(r, share)
            cost.runs -= 1  # a share of a plan run is not an extra run of this task
        attempts: dict[str, int] = defaultdict(int)
        for r in work:
            attempts[r.pool] += 1
        sent_back = sum(1 for r in reviews if r.verified is False)
        tasks.append({
            "id": t.id, "title": t.title, "type": t.type, "difficulty": t.difficulty,
            "status": t.status.value, "success": t.status in _SUCCESS,
            "first_pass": bool(work[0].verified) and sent_back == 0,
            "pool": work[-1].pool, "attempts": dict(attempts),
            "escalations": escalations.get(t.id, 0), "reviews": len(reviews), "sent_back": sent_back,
            "review_issues": sum(review_issues(r, agents_dir / "tasks" / t.id) for r in reviews),
            "cost": cost.as_dict(),
        })

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in tasks:
        groups[(t["type"], t["pool"])].append(t)
    group_rows = []
    for (ttype, pool), ts in sorted(groups.items()):
        ok = [t for t in ts if t["success"]]
        usd = [t["cost"]["usd"] for t in ts if t["cost"]["usd"] is not None]
        group_rows.append({
            "type": ttype, "pool": pool, "tasks": len(ts), "success": len(ok),
            "first_pass": sum(1 for t in ts if t["first_pass"]),
            # all spending (failed tasks included) divided by the successes it bought
            "usd_per_success": round(sum(usd) / len(ok), 6) if ok and usd else None,
            "tokens_per_success": sum(t["cost"]["tokens_in"] + t["cost"]["tokens_out"] for t in ts) // len(ok) if ok else None,
            "seconds_per_success": round(sum(t["cost"]["seconds"] for t in ts) / len(ok), 1) if ok else None,
            "escalations": sum(t["escalations"] for t in ts),
            "review_issues": sum(t["review_issues"] for t in ts),
            "estimated_usd": any(t["cost"]["estimated_usd"] for t in ts),
        })

    roles: dict[tuple[str, str], _Sum] = defaultdict(lambda: _Sum(est))
    for r in runs:
        roles[(r.pool, _role(r))].add(r)
    role_rows = [{"pool": pool, "role": role, **s.as_dict()}
                 for (pool, role), s in sorted(roles.items())]

    others = []
    for owner, rs in sorted(by_owner.items()):
        if owner.startswith(("P-", "D-")):
            s = _Sum(est)
            for r in rs:
                s.add(r)
            others.append({"id": owner, **s.as_dict()})

    return {"since": since, "type": task_type, "groups": group_rows, "roles": role_rows,
            "plans_and_decisions": others, "tasks": tasks}


def _role(r: Run) -> str:
    if r.task_id.startswith("P-"):
        return "plan"
    if r.task_id.startswith("D-"):
        return "decide"
    return r.kind


# -- text output ----------------------------------------------------------------

def _usd(v: float | None, estimated: bool) -> str:
    if v is None:
        return "-"
    return f"{'≈' if estimated else ''}${v:.4f}"


def _tok(n: int | None) -> str:
    if n is None:
        return "-"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    def width(s: str) -> int:  # CJK characters take two columns in a terminal
        return sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    cols = [max(width(x) for x in col) for col in zip(head, *rows)]
    fmt = lambda cells: "  ".join(c + " " * (w - width(c)) for c, w in zip(cells, cols)).rstrip()
    return [fmt(head), fmt(["-" * w for w in cols])] + [fmt(r) for r in rows]


def format_report(rep: dict[str, Any]) -> str:
    out: list[str] = []
    scope = []
    if rep["since"]:
        scope.append("自 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(rep["since"])))
    if rep["type"]:
        scope.append(f"类型 {rep['type']}")
    out.append("== 执行记录报表 ==" + (f"（{'，'.join(scope)}）" if scope else ""))
    if not rep["tasks"] and not rep["roles"]:
        out.append("（还没有运行记录）")
        return "\n".join(out) + "\n"

    out += ["", "== 任务类型 × 池（任务记在最后一次执行它的池上）=="]
    out += _table(
        ["类型", "池", "任务", "成功", "首次通过", "每个成功任务的成本", "token/成功", "耗时/成功", "升级", "审查拦下"],
        [[g["type"], g["pool"], str(g["tasks"]), str(g["success"]),
          f"{g['first_pass']}/{g['tasks']}", _usd(g["usd_per_success"], g["estimated_usd"]),
          _tok(g["tokens_per_success"]),
          f"{g['seconds_per_success']:.0f}s" if g["seconds_per_success"] is not None else "-",
          str(g["escalations"]), str(g["review_issues"])] for g in rep["groups"]])

    out += ["", "== 池 × 角色 =="]
    out += _table(["池", "角色", "次数", "费用", "输入 token", "其中缓存", "未缓存/次", "轮数/次", "输出 token", "耗时",
                   "orch 注入/次"],
                  [[r["pool"], r["role"], str(r["runs"]), _usd(r["usd"], r["estimated_usd"]),
                    _tok(r["tokens_in"]), _tok(r["tokens_cached"]), _tok(r["uncached_avg"]),
                    str(r["calls_avg"]) if r["calls_avg"] is not None else "-",
                    _tok(r["tokens_out"]), f"{r['seconds']:.0f}s", _tok(r["context_avg"])] for r in rep["roles"]])

    if rep["plans_and_decisions"]:
        out += ["", "== 规划与决策（规划成本已平摊进它建出的任务）=="]
        out += _table(["id", "次数", "费用", "输入 token", "耗时"],
                      [[o["id"], str(o["runs"]), _usd(o["usd"], o["estimated_usd"]), _tok(o["tokens_in"]),
                        f"{o['seconds']:.0f}s"] for o in rep["plans_and_decisions"]])

    out += ["", "== 任务明细 =="]
    out += _table(["任务", "状态", "池", "尝试", "首次通过", "审查退回", "成本", "token"],
                  [[t["id"], t["status"], t["pool"], " ".join(f"{p}×{n}" for p, n in t["attempts"].items()),
                    "是" if t["first_pass"] else "否", str(t["sent_back"]), _usd(t["cost"]["usd"], t["cost"]["estimated_usd"]),
                    _tok(t["cost"]["tokens_in"] + t["cost"]["tokens_out"])] for t in rep["tasks"]])
    out += ["", "输入 token 是一次运行里所有模型调用的输入之和（每一轮都会重新发送整段对话），其中缓存命中的部分便宜得多；",
            "未缓存/次才是真正新增的量。轮数/次 = 每次运行的模型调用次数，省 token 主要靠减少轮数。",
            "orch 注入/次：Context Manager 记录的每次运行注入量（V2-1 之后才有）。`orch context <id>` 看单次明细。",
            "说明：≈ 表示 CLI 自己估算的等价美元（订阅额度并不按此扣费）；Codex 不报告美元，只列 token。"]
    return "\n".join(out) + "\n"


def rescan_usage(orch) -> list[tuple[str, int | None, int | None]]:
    """Re-read token usage of past runs from their saved event streams (v0.3).

    Older versions counted opencode's cache reads nowhere and did not record cache hits or model
    calls. Status and results are left alone; only tokens_in / tokens_out / tokens_cached /
    model_calls are recomputed. Returns (run id, old tokens_in, new tokens_in) for changed runs."""
    from .adapters import make_adapter
    from .adapters.base import RunContext
    from .models import Task
    from .proc import ProcResult

    changed = []
    for row in orch.store.db.execute("SELECT id FROM runs ORDER BY id").fetchall():
        run = orch.store.get_run(row["id"])
        if not run or not run.events_path or not Path(run.events_path).is_file():
            continue
        try:
            adapter = make_adapter(orch.cfg, run.pool)
        except KeyError:
            continue
        if adapter.executor in ("fake", "api"):
            continue
        lines = Path(run.events_path).read_text(encoding="utf-8", errors="replace").splitlines()
        ctx = RunContext(task=Task(id=run.task_id, title=""), worktree=orch.project,
                         events_path=Path(run.events_path), timeout_s=0)
        res = adapter.parse(ProcResult(exit_code=run.exit_code or 0, tail=lines), ctx)
        new = (res.tokens_in, res.tokens_out, res.tokens_cached, res.model_calls)
        old = (run.tokens_in, run.tokens_out, run.tokens_cached, run.model_calls)
        if new != old and res.tokens_in is not None:
            changed.append((run.id, run.tokens_in, res.tokens_in))
            run.tokens_in, run.tokens_out, run.tokens_cached, run.model_calls = new
            orch.store.save_run(run)
    return changed
