"""Rule-table router + circuit breaker.

v0.1 routing = the ordered pool list for (task type, difficulty) from orch.toml.
The task's `ladder` is its escalation level: pools before it are never used again for
that task. Pools whose breaker is open or whose CLI is missing are skipped for now.
"""
from __future__ import annotations

import time

from .adapters import Adapter, make_adapter
from .config import Config
from .models import OPEN_IMMEDIATELY, RunStatus, Task
from .store import Store


class Breakers:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg

    def is_open(self, pool: str, now: float | None = None) -> bool:
        row = self.store.breaker(pool)
        return bool(row) and (row["open_until"] or 0) > (now or time.time())

    def record_failure(self, pool: str, status: RunStatus, resets_at: float | None) -> None:
        row = self.store.breaker(pool)
        failures = (row["failures"] if row else 0) + 1
        now = time.time()
        cooldown = float(self.cfg.esc.get("breaker_cooldown_minutes", 30)) * 60
        open_until = row["open_until"] if row else 0
        if status in OPEN_IMMEDIATELY:
            open_until = resets_at if (resets_at and resets_at > now) else now + cooldown
        elif failures >= int(self.cfg.esc.get("breaker_threshold", 2)):
            open_until = now + cooldown
        self.store.set_breaker(pool, failures, open_until, status.value)
        if open_until and open_until > now:
            until = time.strftime("%H:%M", time.localtime(open_until))
            self.store.log("breaker_open", f"{pool} open until {until} ({status.value})")

    def record_success(self, pool: str) -> None:
        if self.store.breaker(pool):
            self.store.set_breaker(pool, 0, 0, "")

    def force_open(self, pool: str, minutes: float, reason: str = "manual") -> float:
        row = self.store.breaker(pool)
        until = time.time() + minutes * 60
        self.store.set_breaker(pool, row["failures"] if row else 0, until, reason)
        self.store.log("breaker_open", f"{pool} opened by hand for {minutes:g} min")
        return until

    def reset(self, pool: str | None = None) -> None:
        pools = [pool] if pool else [r["pool"] for r in self.store.all_breakers()]
        for p in pools:
            self.store.set_breaker(p, 0, 0, "manual reset")


class Router:
    def __init__(self, cfg: Config, store: Store, breakers: Breakers):
        self.cfg = cfg
        self.store = store
        self.breakers = breakers

    def route(self, task: Task) -> list[str]:
        return self.cfg.route_for(task.type, task.difficulty)

    def quota_reserved(self, pool: str, task: Task) -> bool:
        """Keep the last `reserve` share of a subscription window for critical tasks.

        Uses the utilization the CLI itself reported on its last run (Claude Code puts it in
        rate_limit_event). Pools without such data are never held back here.
        """
        reserve = float(self.cfg.pools.get(pool, {}).get("reserve", 0) or 0)
        if reserve <= 0 or task.risk == "critical":
            return False
        quota = self.store.latest_quota(pool)
        if not quota:
            return False
        worst = max(w.get("utilization", 0) for w in quota.values())
        if worst >= 1 - reserve:
            self.store.log(
                "quota_reserve",
                f"{pool}: {worst:.0%} used, last {reserve:.0%} kept for critical tasks",
                task_id=task.id,
            )
            return True
        return False

    WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

    def pacing_blocked(self, pool: str, task: Task, now: float | None = None) -> bool:
        """Pacing gate (v0.3): project each subscription window's use at its reset from the pace so far.

        u = share of the window used, f = share of the window's time gone; at this pace the window
        ends near u / f. Up to `soft` (80%) every task may use the pool; up to `hard` (100%) only
        hard work (difficulty L, or risk high / critical); beyond that only critical tasks. Early in a
        window the pace says little, so f is never taken below `min_elapsed` (10%)."""
        pcfg = self.cfg.data.get("pacing", {})
        if not pcfg.get("enabled", True) or task.risk == "critical":
            return False
        quota = self.store.latest_quota(pool)
        if not quota:
            return False
        now = now or time.time()
        soft, hard = float(pcfg.get("soft", 0.8)), float(pcfg.get("hard", 1.0))
        min_f = float(pcfg.get("min_elapsed", 0.1))
        important = task.difficulty == "L" or task.risk in ("high", "critical")
        for name, w in quota.items():
            length = self.WINDOW_SECONDS.get(name)
            if not length or not w.get("resets_at"):
                continue
            f = max(min_f, min(1.0, 1 - (float(w["resets_at"]) - now) / length))
            projected = float(w.get("utilization", 0)) / f
            if projected <= soft:
                continue
            if projected <= hard and important:
                continue
            who = "只放行 L 级或高风险任务" if projected <= hard else "只放行 critical 任务"
            self.store.log("pacing", f"{pool} {name}: 按当前速度重置时约用到 {projected:.0%}，{who}",
                           task_id=task.id)
            return True
        return False

    def pick(self, task: Task) -> tuple[int, str, Adapter] | None:
        return self.pick_from(task, self.route(task), task.ladder)

    def pick_from(self, task: Task, route: list[str], start: int = 0,
                  needs_tools: bool = True) -> tuple[int, str, Adapter] | None:
        """needs_tools=False for steps that only read text they are given (review, judge): only then
        may a tool-free pool (direct API) be picked."""
        for i in range(start, len(route)):
            pool = route[i]
            if pool not in self.cfg.pools:
                self.store.log("route_skip", f"{pool}: not defined in [pools]", task_id=task.id)
                continue
            if self.breakers.is_open(pool):
                continue
            if self.quota_reserved(pool, task) or self.pacing_blocked(pool, task):
                continue
            try:
                adapter = make_adapter(self.cfg, pool)
            except KeyError as e:
                self.store.log("route_skip", str(e), task_id=task.id)
                continue
            if needs_tools and adapter.tool_free:
                continue
            if not adapter.available():
                self.breakers.record_failure(pool, RunStatus.UNAVAILABLE, None)
                continue
            return i, pool, adapter
        return None
