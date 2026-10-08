"""Core data types. Stdlib only (dataclasses) so the package runs without pip installs."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    QUEUED = "queued"        # waiting to run (or to be retried)
    RUNNING = "running"      # an executor is working on it
    VERIFIED = "verified"    # verify commands passed; waiting for merge
    MERGED = "merged"        # squash-merged into the integration branch
    BLOCKED = "blocked"      # no executor left / agent said BLOCKED -> needs a human
    FAILED = "failed"        # ran out of max_runs
    CANCELLED = "cancelled"  # dropped by the user (`orch task cancel`); `orch task retry` brings it back


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"        # agent finished normally; verification decides success
    TASK_FAILED = "task_failed"    # agent ended with an error result (e.g. max turns)
    RATE_LIMITED = "rate_limited"  # quota / rate limit hit
    AUTH_ERROR = "auth_error"      # not logged in, bad key
    TIMEOUT = "timeout"            # orchestrator stopped it after run_timeout_minutes
    CRASHED = "crashed"            # non-zero exit without a recognisable reason
    UNAVAILABLE = "unavailable"    # executable not found
    INTERRUPTED = "interrupted"    # orchestrator died while the run was in progress


# Failures caused by the executor/infrastructure rather than by the task.
# These feed the circuit breaker and trigger a handoff to the next pool.
INFRA_FAILURES = {
    RunStatus.RATE_LIMITED,
    RunStatus.AUTH_ERROR,
    RunStatus.CRASHED,
    RunStatus.UNAVAILABLE,
    RunStatus.INTERRUPTED,
}

# Infra failures that open the breaker immediately (no point retrying soon).
OPEN_IMMEDIATELY = {RunStatus.RATE_LIMITED, RunStatus.AUTH_ERROR, RunStatus.UNAVAILABLE}


@dataclass
class Task:
    id: str
    title: str
    spec: str = ""
    type: str = "code_change"
    difficulty: str = "M"          # S / M / L
    risk: str = "normal"           # low / normal / high / critical
    scope: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    max_runs: int = 4
    status: TaskStatus = TaskStatus.QUEUED
    ladder: int = 0                # index into the routing list (escalation level)
    verify_failures: int = 0       # consecutive verify failures at the current level
    runs_count: int = 0
    branch: str = ""
    worktree: str = ""
    note: str = ""                 # last orchestrator note (why blocked, merge conflict, ...)
    review_rounds: int = 0         # times a reviewer sent the task back (M5)
    review: str = "auto"           # auto | always | never (V2-3; auto = config decides)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class RunResult:
    status: RunStatus
    session_id: str | None = None
    summary: str = ""
    est_cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None   # part of tokens_in served from the provider's prompt cache
    model_calls: int | None = None     # model round trips in the run (each re-sends the context)
    resets_at: float | None = None     # epoch seconds, when a rate limit resets
    # Subscription windows reported by the CLI, e.g.
    # {"five_hour": {"utilization": 0.02, "resets_at": 1791390000}, "seven_day": {...}}
    quota: dict | None = None
    exit_code: int | None = None
    events_path: Path | None = None
    error: str = ""


@dataclass
class Run:
    id: str
    task_id: str
    pool: str
    executor: str
    status: RunStatus = RunStatus.RUNNING
    session_id: str | None = None
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    est_cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None   # tokens_in = ALL input processed, cached part included
    model_calls: int | None = None
    resets_at: float | None = None
    quota: str = ""                    # JSON of RunResult.quota
    summary: str = ""
    error: str = ""
    events_path: str = ""
    verified: bool | None = None       # work: passed acceptance; review: approved
    resumed: bool = False
    kind: str = "work"                 # work | review | plan | decide
    issues_major: int | None = None    # review: blocker + major issues raised (V2-2)
    issues_minor: int | None = None
    context_tokens: int | None = None  # tokens orch injected (Context Manager estimate, V2-1)
    context_manifest: str = ""         # JSON: parts, budget, retrieval, compression
