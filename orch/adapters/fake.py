"""Fake executor for the offline demo and tests: runs a Python script inside the worktree.

The script plays the role of an agent: it edits files and prints JSON lines such as
  {"type": "result", "session_id": "fake-1", "text": "done"}
  {"type": "error", "error": "rate_limit", "resets_at": 1760000000}
Costs nothing, so the whole orchestration loop can be exercised before touching quotas.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..models import RunResult, RunStatus
from ..proc import ProcResult
from .base import Adapter, RunContext, iter_json


class FakeAdapter(Adapter):
    executor = "fake"

    @property
    def script(self) -> Path:
        p = Path(self.pool_cfg.get("script", ""))
        return p if p.is_absolute() else (self.project / p)

    @property
    def command(self) -> str:
        return sys.executable

    def available(self) -> bool:
        return self.script.is_file()

    def build_cmd(self, exe: str, ctx: RunContext) -> list[str]:
        cmd = [exe, str(self.script)]
        if ctx.resume_session:
            cmd += ["--resume", ctx.resume_session]
        return cmd

    def parse(self, pr: ProcResult, ctx: RunContext) -> RunResult:
        res = RunResult(status=RunStatus.CRASHED)
        for ev in iter_json(pr.tail):
            if ev.get("session_id"):
                res.session_id = ev["session_id"]
            if ev.get("type") == "result":
                res.status = RunStatus.COMPLETED
                res.summary = str(ev.get("text", ""))
                res.est_cost_usd = float(ev.get("cost_usd", 0.0))
                if isinstance(ev.get("tokens_in"), int):
                    res.tokens_in = ev["tokens_in"]
            elif ev.get("type") == "error":
                res.error = str(ev.get("error", ""))
                res.status = RunStatus.RATE_LIMITED if "rate" in res.error else RunStatus.CRASHED
                if isinstance(ev.get("resets_at"), (int, float)):
                    res.resets_at = float(ev["resets_at"])
        if res.status == RunStatus.COMPLETED and pr.exit_code not in (0, None):
            res.status = RunStatus.CRASHED
        return res
