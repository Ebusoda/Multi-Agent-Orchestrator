"""Adapter contract: every executor (CLI harness + model) is driven the same way."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from ..models import RunResult, RunStatus, Task
from ..proc import ProcResult, resolve_command, run_streaming

# Short, ASCII-only instruction passed on the command line. The real prompt lives in
# .task/PROMPT.md inside the worktree (avoids Windows .cmd quoting problems and lets the
# next agent see exactly what the previous one was told).
INSTRUCTION = (
    "Read the file .task/PROMPT.md in the current directory and carry out the assignment "
    "it describes. Follow its rules exactly."
)

_RATE = re.compile(
    r"rate[ _-]?limit|usage limit|quota|\b429\b|too many requests|limit reached|insufficient_quota", re.I
)
_AUTH = re.compile(
    r"authenticat|unauthori[sz]ed|\b401\b|invalid[ _-]api[ _-]key|not logged in|please (?:run )?/?log ?in"
    r"|oauth token|credentials? (?:not found|missing|expired)",
    re.I,
)


@dataclass
class RunContext:
    task: Task
    worktree: Path
    events_path: Path
    timeout_s: float
    model: str = ""
    resume_session: str | None = None
    instruction: str = INSTRUCTION
    on_start: Any = None  # callback(pid)
    readonly: bool = False  # planning / review runs: the agent must not modify files
    stdin_text: str | None = None  # prompt delivered on stdin (executor option prompt_via_stdin)


def iter_json(lines: list[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def classify_text(text: str) -> RunStatus | None:
    if _RATE.search(text):
        return RunStatus.RATE_LIMITED
    if _AUTH.search(text):
        return RunStatus.AUTH_ERROR
    return None


_PROMPT_FILE = re.compile(r"\.task/[A-Za-z0-9_.-]+\.md")
STDIN_INSTRUCTION = (
    "The assignment from the orchestrator is in the <stdin> block below (a copy is saved as {file}; "
    "you do not need to open it). Carry it out and follow its rules exactly."
)


class Adapter:
    """Subclasses implement build_cmd() and parse()."""

    executor = "base"
    default_vendor = ""  # company behind the model; reviews must come from a different one
    tool_free = False    # True = cannot edit files or run commands (direct API): review / judge only

    def __init__(self, pool: str, pool_cfg: dict[str, Any], exec_cfg: dict[str, Any], project: Path):
        self.pool = pool
        self.pool_cfg = pool_cfg
        self.exec_cfg = exec_cfg
        self.project = project

    @property
    def command(self) -> str:
        return self.exec_cfg.get("command", self.executor)

    @property
    def model(self) -> str:
        return self.pool_cfg.get("model", "") or ""

    @property
    def vendor(self) -> str:
        """`vendor = "..."` in the pool overrides the executor's default (e.g. Claude Code on a
        non-Anthropic model)."""
        return str(self.pool_cfg.get("vendor") or self.default_vendor or self.pool).lower()

    def available(self) -> bool:
        return resolve_command(self.command) is not None

    def supports_resume(self) -> bool:
        return True

    def extra_env(self) -> dict[str, str]:
        """Environment variables this executor needs on top of the user's (opencode: its API key)."""
        return {}

    def build_cmd(self, exe: str, ctx: RunContext) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def parse(self, pr: ProcResult, ctx: RunContext) -> RunResult:  # pragma: no cover
        raise NotImplementedError

    def run(self, ctx: RunContext) -> RunResult:
        exe = resolve_command(self.command)
        if not exe:
            return RunResult(status=RunStatus.UNAVAILABLE, error=f"command not found: {self.command}")
        if self.exec_cfg.get("prompt_via_stdin") and ctx.stdin_text is None:
            # One model call fewer: the prompt arrives with the first message instead of being read
            # by a tool call. Codex appends piped stdin as a <stdin> block; `claude -p` reads it too.
            m = _PROMPT_FILE.search(ctx.instruction)
            p = ctx.worktree / (m.group(0) if m else ".task/PROMPT.md")
            if p.is_file():
                ctx = replace(ctx, instruction=STDIN_INSTRUCTION.format(file=p.relative_to(ctx.worktree).as_posix()),
                              stdin_text=p.read_text(encoding="utf-8-sig", errors="replace"))
        cmd = self.build_cmd(exe, ctx)
        pr = run_streaming(cmd, ctx.worktree, ctx.events_path, ctx.timeout_s, env=self.extra_env() or None,
                           on_start=ctx.on_start, stdin_text=ctx.stdin_text)
        if pr.not_found:
            return RunResult(status=RunStatus.UNAVAILABLE, error=f"cannot start: {cmd[0]}")
        res = self.parse(pr, ctx)
        res.exit_code = pr.exit_code
        res.events_path = ctx.events_path
        if pr.interrupted:
            res.status = RunStatus.INTERRUPTED
        elif pr.timed_out:
            res.status = RunStatus.TIMEOUT
        elif res.status == RunStatus.COMPLETED and pr.exit_code not in (0, None):
            res.status = classify_text(res.error + "\n" + pr.stderr_tail) or RunStatus.CRASHED
        if res.status != RunStatus.COMPLETED and not res.error:
            res.error = pr.stderr_tail[-2000:]
        return res

    # Generic fallback used by adapters whose output format is not known for sure.
    @staticmethod
    def fallback_status(pr: ProcResult, text: str) -> RunStatus:
        if pr.exit_code == 0:
            return RunStatus.COMPLETED
        return classify_text(text + "\n" + pr.stderr_tail) or RunStatus.CRASHED
