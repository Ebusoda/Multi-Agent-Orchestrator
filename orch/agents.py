"""Agent control panel: every pool's state in one place, plus the controls a person needs.

`panel()` returns plain JSON-ready data, so `orch agents`, the MCP server and a web page can all show
the same thing. Each pool ("agent") gets: what it is working on, today's and the last 7 days' use,
its subscription windows, its recent success rate, its last error and whether it is paused.

Controls:
- pause / resume: a pause is a breaker opened by hand with reason "paused"; it lasts until resumed
  (or for the given minutes). Running work is not interrupted; the pool just gets nothing new.
- swap: pin a task to a pool. The pinned pool goes first in the task's route; if it fails the task
  escalates along the normal route as usual.
- retry / cancel stay where they were (`orch task retry|cancel`).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import RunStatus, TaskStatus
from .i18n import tr

if TYPE_CHECKING:
    from .scheduler import Orchestrator

PAUSED = "paused"
FOREVER_MINUTES = 100 * 365 * 24 * 60  # "until resumed"
RECENT = 10  # work runs that make up the recent success rate


def _last_line(text: str, limit: int = 200) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


def _usage(rows) -> dict:
    return {r["pool"]: {"runs": r["runs"], "cost_usd": round(r["cost"] or 0, 4),
                        "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0} for r in rows}


def panel(orch: "Orchestrator", now: float | None = None, manual: bool = True) -> dict:
    """manual=True also reads today's Claude Code / Codex sessions on this computer (orch/sessions.py:
    times, folders and token counts only), so the panel shows use outside orch too."""
    now = now or time.time()
    local = None
    if manual:
        from . import sessions

        local = sessions.scan(days=1, now=now)
    store = orch.store
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    today = _usage(store.usage_since(midnight))
    week = _usage(store.usage_since(now - 7 * 86400))
    breakers = {r["pool"]: r for r in store.all_breakers()}
    tasks = {t.id: t for t in store.list_tasks()}
    running: dict[str, list] = {}
    for r in store.running_runs():
        t = tasks.get(r.task_id)
        running.setdefault(r.pool, []).append({
            "run": r.id, "task": r.task_id, "title": t.title if t else "", "kind": r.kind,
            "started_at": r.started_at})
    zero = {"runs": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    agents = []
    for pool, pcfg in orch.cfg.pools.items():
        rows = store.db.execute(
            "SELECT * FROM runs WHERE pool=? AND kind='work' AND status!=? ORDER BY id DESC LIMIT ?",
            (pool, RunStatus.RUNNING.value, RECENT)).fetchall()
        ok = sum(1 for r in rows if r["verified"])
        err = store.db.execute(  # the last run that went wrong: the executor failed, or verify did
            "SELECT id, task_id, status, error, summary, verified, ended_at FROM runs WHERE pool=? "
            "AND (status NOT IN (?, ?) OR (kind='work' AND verified=0)) ORDER BY id DESC LIMIT 1",
            (pool, RunStatus.RUNNING.value, RunStatus.COMPLETED.value)).fetchone()
        last = store.db.execute("SELECT MAX(started_at) AS t FROM runs WHERE pool=?", (pool,)).fetchone()
        b = breakers.get(pool)
        closed_until = (b["open_until"] or 0) if b else 0
        paused = closed_until > now and (b["reason"] or "") == PAUSED
        if paused:
            state = "paused"
        elif closed_until > now:
            state = "cooling"  # the breaker opened by itself (rate limit, crashes) or by `breaker open`
        elif running.get(pool):
            state = "working"
        else:
            state = "idle"
        agents.append({
            "pool": pool,
            "executor": str(pcfg.get("executor", "")),
            "model": str(pcfg.get("model", "") or ""),
            "billing": "api" if orch.is_api_pool(pool) else "subscription",
            "state": state,
            "until": closed_until if closed_until > now else None,
            "until_forever": closed_until - now > FOREVER_MINUTES * 60 / 2,
            "reason": (b["reason"] or "") if b and closed_until > now else "",
            "current": running.get(pool, []),
            "today": today.get(pool, dict(zero)),
            "week": week.get(pool, dict(zero)),
            # Codex runs started by orch report no windows; its session logs do
            "quota": store.latest_quota(pool) or (local["codex_quota"] if local and pcfg.get("executor") == "codex"
                                                  else None),
            "recent": {"runs": len(rows), "ok": ok, "rate": round(ok / len(rows), 2) if rows else None},
            "last_error": {"run": err["id"], "task": err["task_id"],
                           "status": "verify_failed" if err["status"] == RunStatus.COMPLETED.value else err["status"],
                           "at": err["ended_at"],
                           "text": _last_line(err["error"]) or _last_line(err["summary"])} if err else None,
            "last_run_at": last["t"] if last else None,
        })
    counts: dict[str, int] = {}
    for t in tasks.values():
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    attention = [{"task": t.id, "title": t.title, "status": t.status.value, "note": t.note[:200]}
                 for t in tasks.values() if t.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)]
    queued = []
    for t in tasks.values():
        if t.status == TaskStatus.QUEUED:
            route = orch.router.route(t)
            queued.append({"task": t.id, "title": t.title, "pin": t.pin,
                           "next": route[t.ladder] if t.ladder < len(route) else None})
    return {"project": str(orch.project), "at": now, "agents": agents, "tasks": counts,
            "manual": {"summary": local["summary"], "active": [
                {k: x[k] for k in ("tool", "id", "project", "model", "last_at", "tokens_today")}
                for x in local["sessions"] if x["active"] and x["source"] == "manual"]} if local else None,
            "attention": attention, "queued": queued}


def _check_pool(orch: "Orchestrator", pool: str) -> None:
    if pool not in orch.cfg.pools:
        raise ValueError(tr("没有这个 agent（池）: {0}（可选: {1}）", pool, ', '.join(orch.cfg.pools)))


def pause(orch: "Orchestrator", pool: str, minutes: float | None = None) -> float:
    _check_pool(orch, pool)
    until = orch.breakers.force_open(pool, minutes or FOREVER_MINUTES, reason=PAUSED)
    orch.store.log("agent_paused", pool + (f" for {minutes:g} min" if minutes else " until resumed"))
    return until


def resume(orch: "Orchestrator", pool: str) -> None:
    _check_pool(orch, pool)
    orch.breakers.reset(pool)
    orch.store.log("agent_resumed", pool)


def swap(orch: "Orchestrator", task_id: str, pool: str) -> str:
    """Pin a task to `pool` and queue it again from the start of its (now pinned) route."""
    _check_pool(orch, pool)
    t = orch.store.get_task(task_id)
    if not t:
        raise ValueError(tr("没有 {0}", task_id))
    if t.status in (TaskStatus.RUNNING, TaskStatus.VERIFIED, TaskStatus.MERGED):
        raise ValueError(tr("{0} 状态是 {1}，不能换人", task_id, t.status.value)
                         + (tr("（等这次运行结束再换）") if t.status == TaskStatus.RUNNING else ""))
    old = t.pin
    t.pin, t.ladder, t.verify_failures = pool, 0, 0
    if t.status != TaskStatus.QUEUED:
        t.status, t.runs_count, t.review_rounds = TaskStatus.QUEUED, 0, 0
    t.note = f"swapped to {pool} by hand"
    orch.store.save_task(t)
    orch.store.log("task_swapped", f"{old or '(route)'} -> {pool}", task_id=task_id)
    return t.note
