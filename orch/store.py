"""SQLite state store. Only the orchestrator writes here; agents never touch it."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterable

from .models import Run, RunStatus, Task, TaskStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, spec TEXT, type TEXT, difficulty TEXT, risk TEXT,
    scope TEXT, verify TEXT, depends_on TEXT, max_runs INTEGER, status TEXT,
    ladder INTEGER, verify_failures INTEGER, runs_count INTEGER,
    branch TEXT, worktree TEXT, note TEXT, created_at REAL, updated_at REAL, review_rounds INTEGER DEFAULT 0,
    review TEXT DEFAULT 'auto', pin TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, task_id TEXT, pool TEXT, executor TEXT, status TEXT,
    session_id TEXT, pid INTEGER, started_at REAL, ended_at REAL, exit_code INTEGER,
    est_cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER, resets_at REAL,
    summary TEXT, error TEXT, events_path TEXT, verified INTEGER, resumed INTEGER, quota TEXT,
    kind TEXT DEFAULT 'work'
);
CREATE TABLE IF NOT EXISTS breakers (
    pool TEXT PRIMARY KEY, failures INTEGER DEFAULT 0, open_until REAL DEFAULT 0, reason TEXT
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, run_id TEXT, kind TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, goal TEXT, status TEXT, pool TEXT, plan_json TEXT,
    task_map TEXT, error TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, question TEXT, status TEXT, title TEXT, adr_path TEXT, repo_path TEXT,
    detail TEXT, error TEXT, created_at REAL, updated_at REAL
);
-- V2-2: one row per run with the task attributes reports group by (`orch report`)
CREATE VIEW IF NOT EXISTS run_records AS
    SELECT r.*, t.type AS task_type, t.difficulty AS task_difficulty, t.risk AS task_risk,
           t.status AS task_status
    FROM runs r LEFT JOIN tasks t ON t.id = r.task_id;
"""

_JSON_TASK_FIELDS = {"scope", "verify", "depends_on"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "quota" not in cols:
            self.db.execute("ALTER TABLE runs ADD COLUMN quota TEXT")
        if "kind" not in cols:  # M5
            self.db.execute("ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'work'")
        for col in ("tokens_cached", "model_calls"):  # v0.3
            if col not in cols:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER")
        if "context_tokens" not in cols:  # V2-1
            self.db.execute("ALTER TABLE runs ADD COLUMN context_tokens INTEGER")
        if "context_manifest" not in cols:
            self.db.execute("ALTER TABLE runs ADD COLUMN context_manifest TEXT")
        for col in ("issues_major", "issues_minor"):  # V2-2
            if col not in cols:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER")
        tcols = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "review_rounds" not in tcols:  # M5
            self.db.execute("ALTER TABLE tasks ADD COLUMN review_rounds INTEGER DEFAULT 0")
        if "review" not in tcols:  # V2-3
            self.db.execute("ALTER TABLE tasks ADD COLUMN review TEXT DEFAULT 'auto'")
        if "pin" not in tcols:  # agent panel
            self.db.execute("ALTER TABLE tasks ADD COLUMN pin TEXT DEFAULT ''")

    def close(self) -> None:
        self.db.close()

    # -- ids -----------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        key = f"seq_{prefix}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            n = int(row["value"]) + 1 if row else 1
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(n)))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return f"{prefix}-{n:04d}"

    # -- tasks ---------------------------------------------------------------
    def add_task(self, task: Task) -> Task:
        if not task.id:
            task.id = self._next_id("T")
        self._upsert_task(task)
        self.log("task_added", f"{task.id} {task.title}", task_id=task.id)
        return task

    def _upsert_task(self, task: Task) -> None:
        task.updated_at = time.time()
        row: dict[str, Any] = {}
        for f in fields(Task):
            v = getattr(task, f.name)
            if f.name in _JSON_TASK_FIELDS:
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, TaskStatus):
                v = v.value
            row[f.name] = v
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.db.execute(f"INSERT OR REPLACE INTO tasks({cols}) VALUES({marks})", tuple(row.values()))

    def save_task(self, task: Task) -> None:
        self._upsert_task(task)

    @staticmethod
    def _task_from_row(r: sqlite3.Row) -> Task:
        kw: dict[str, Any] = {}
        for f in fields(Task):
            v = r[f.name]
            if f.name in _JSON_TASK_FIELDS:
                v = json.loads(v or "[]")
            elif f.name == "status":
                v = TaskStatus(v)
            elif v is None and f.name in ("note", "branch", "worktree", "spec", "pin"):
                v = ""
            elif v is None and f.name == "review_rounds":
                v = 0
            elif v is None and f.name == "review":
                v = "auto"
            kw[f.name] = v
        return Task(**kw)

    def get_task(self, task_id: str) -> Task | None:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(r) if r else None

    def list_tasks(self, statuses: Iterable[TaskStatus] | None = None) -> list[Task]:
        rows = self.db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        tasks = [self._task_from_row(r) for r in rows]
        if statuses is not None:
            wanted = set(statuses)
            tasks = [t for t in tasks if t.status in wanted]
        return tasks

    # -- runs ----------------------------------------------------------------
    def create_run(self, task_id: str, pool: str, executor: str, resumed: bool, kind: str = "work") -> Run:
        run = Run(id=self._next_id("R"), task_id=task_id, pool=pool, executor=executor, resumed=resumed,
                  kind=kind)
        self.save_run(run)
        return run

    def save_run(self, run: Run) -> None:
        row: dict[str, Any] = {}
        for f in fields(Run):
            v = getattr(run, f.name)
            if isinstance(v, RunStatus):
                v = v.value
            elif isinstance(v, bool):
                v = int(v)
            row[f.name] = v
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.db.execute(f"INSERT OR REPLACE INTO runs({cols}) VALUES({marks})", tuple(row.values()))

    @staticmethod
    def _run_from_row(r: sqlite3.Row) -> Run:
        kw: dict[str, Any] = {f.name: r[f.name] for f in fields(Run)}
        kw["status"] = RunStatus(kw["status"])
        kw["verified"] = None if kw["verified"] is None else bool(kw["verified"])
        kw["resumed"] = bool(kw["resumed"])
        kw["summary"] = kw["summary"] or ""
        kw["error"] = kw["error"] or ""
        kw["events_path"] = kw["events_path"] or ""
        kw["quota"] = kw["quota"] or ""
        kw["kind"] = kw["kind"] or "work"
        kw["context_manifest"] = kw["context_manifest"] or ""
        return Run(**kw)

    def runs_for(self, task_id: str, kind: str | None = None) -> list[Run]:
        rows = self.db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        runs = [self._run_from_row(r) for r in rows]
        return [r for r in runs if r.kind == kind] if kind else runs

    def get_run(self, run_id: str) -> Run | None:
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_from_row(r) if r else None

    def running_runs(self) -> list[Run]:
        rows = self.db.execute("SELECT * FROM runs WHERE status=?", (RunStatus.RUNNING.value,)).fetchall()
        return [self._run_from_row(r) for r in rows]

    def recent_runs(self, limit: int = 10) -> list[Run]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._run_from_row(r) for r in rows]

    def usage_since(self, since: float) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT pool, COUNT(*) AS runs, SUM(COALESCE(est_cost_usd,0)) AS cost,
                      SUM(COALESCE(tokens_in,0)) AS tin, SUM(COALESCE(tokens_out,0)) AS tout
               FROM runs WHERE started_at >= ? GROUP BY pool ORDER BY pool""",
            (since,),
        ).fetchall()

    def latest_quota(self, pool: str) -> dict | None:
        """Most recent subscription-window report for a pool (windows already reset are dropped)."""
        row = self.db.execute(
            "SELECT quota FROM runs WHERE pool=? AND quota IS NOT NULL AND quota != '' ORDER BY id DESC LIMIT 1",
            (pool,),
        ).fetchone()
        if not row:
            return None
        now = time.time()
        windows = {k: v for k, v in json.loads(row["quota"]).items() if (v.get("resets_at") or 0) > now}
        return windows or None

    # -- plans (M4) ----------------------------------------------------------
    def new_plan(self, goal: str) -> str:
        pid = self._next_id("P")
        now = time.time()
        self.db.execute(
            "INSERT INTO plans(id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (pid, goal, "planning", now, now),
        )
        self.log("plan_started", goal[:200], task_id=pid)
        return pid

    def save_plan(self, pid: str, status: str, pool: str, plan, error: str = "") -> None:
        data = (
            json.dumps({"summary": plan.summary, "tasks": plan.tasks, "warnings": plan.warnings},
                       ensure_ascii=False)
            if plan is not None else None
        )
        self.db.execute(
            "UPDATE plans SET status=?, pool=?, plan_json=?, error=?, updated_at=? WHERE id=?",
            (status, pool, data, error, time.time(), pid),
        )
        self.log("plan_" + status, error[:200] if error else "", task_id=pid)

    def set_plan_status(self, pid: str, status: str, task_map: str | None = None) -> None:
        self.db.execute(
            "UPDATE plans SET status=?, task_map=COALESCE(?, task_map), updated_at=? WHERE id=?",
            (status, task_map, time.time(), pid),
        )
        self.log("plan_" + status, "", task_id=pid)

    def get_plan(self, pid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()

    def latest_plan(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()

    def list_plans(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM plans ORDER BY id").fetchall()

    # -- decisions (M5) ------------------------------------------------------
    def new_decision(self, question: str) -> str:
        did = self._next_id("D")
        now = time.time()
        self.db.execute(
            "INSERT INTO decisions(id, question, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (did, question, "running", now, now),
        )
        self.log("decide_started", question[:200], task_id=did)
        return did

    def update_decision(self, did: str, **values: Any) -> None:
        allowed = {"status", "title", "adr_path", "repo_path", "detail", "error"}
        bad = set(values) - allowed
        if bad:
            raise ValueError(f"unknown decision fields: {bad}")
        values["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE decisions SET {cols} WHERE id=?", (*values.values(), did))
        if "status" in values:
            self.log("decide_" + str(values["status"]), str(values.get("error") or "")[:200], task_id=did)

    def get_decision(self, did: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()

    def latest_decision(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone()

    def list_decisions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions ORDER BY id").fetchall()

    # -- breakers ------------------------------------------------------------
    def breaker(self, pool: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM breakers WHERE pool=?", (pool,)).fetchone()

    def set_breaker(self, pool: str, failures: int, open_until: float, reason: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO breakers(pool, failures, open_until, reason) VALUES(?,?,?,?)",
            (pool, failures, open_until, reason),
        )

    def all_breakers(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM breakers ORDER BY pool").fetchall()

    # -- log -----------------------------------------------------------------
    def log(self, kind: str, message: str, task_id: str | None = None, run_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO log(ts, task_id, run_id, kind, message) VALUES(?,?,?,?,?)",
            (time.time(), task_id, run_id, kind, message),
        )

    def tail_log(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
