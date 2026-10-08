"""Claude Code CLI (`claude -p ... --output-format stream-json`).

Uses the user's own login of the unmodified `claude` binary (subscription or API key,
whatever `claude` itself is configured with). The orchestrator never reads Claude
credentials. Do NOT add `--bare` here unless you use an API key: bare mode ignores the
subscription login.
"""
from __future__ import annotations

from ..models import RunResult, RunStatus
from ..proc import ProcResult
from .base import Adapter, RunContext, classify_text, iter_json


class ClaudeCodeAdapter(Adapter):
    executor = "claude_code"
    default_vendor = "anthropic"

    def build_cmd(self, exe: str, ctx: RunContext) -> list[str]:
        args = list(self.exec_cfg.get("args", []))
        if ctx.readonly:
            # dontAsk: anything that would need approval is denied; no edit tools are offered
            if "--permission-mode" in args:
                args[args.index("--permission-mode") + 1] = "dontAsk"
            else:
                args += ["--permission-mode", "dontAsk"]
        cmd = [exe, "-p", ctx.instruction, *args]
        if ctx.readonly:
            ro = self.exec_cfg.get("readonly_tools") or ["Read", "Glob", "Grep", "Bash", "PowerShell"]
            cmd += ["--tools", ",".join(ro), "--allowedTools", "Read,Glob,Grep"]
            if self.model:
                cmd += ["--model", self.model]
            if ctx.resume_session:
                cmd += ["--resume", ctx.resume_session]
            return cmd
        available = self.exec_cfg.get("tools") or []
        if available:
            # restrict the built-in tool set (no cron / web / remote tools in orchestrated runs)
            cmd += ["--tools", ",".join(available)]
        tools = self.exec_cfg.get("allowed_tools") or []
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        if self.model:
            cmd += ["--model", self.model]
        if ctx.resume_session:
            cmd += ["--resume", ctx.resume_session]
        return cmd

    def parse(self, pr: ProcResult, ctx: RunContext) -> RunResult:
        res = RunResult(status=RunStatus.CRASHED)
        result_seen = False
        rejected = False
        errors: list[str] = []
        for ev in iter_json(pr.tail):
            t = ev.get("type")
            if ev.get("session_id"):
                res.session_id = ev["session_id"]
            if t == "rate_limit_event":
                # Seen on Claude Code 2.1.292 (Windows, subscription):
                # {"rate_limit_info": {"status": "allowed", "resetsAt": ..., "rateLimitType": "five_hour",
                #   "unifiedWindows": {"five_hour": {"utilization": 0.02, "resetsAt": ...},
                #                      "seven_day": {"utilization": 0.32, "resetsAt": ...}}}}
                info = ev.get("rate_limit_info") or ev
                status = str(info.get("status", ""))
                resets = info.get("resetsAt") or info.get("resets_at")
                if isinstance(resets, (int, float)):
                    res.resets_at = float(resets)
                if status == "rejected":
                    rejected = True
                windows = info.get("unifiedWindows")
                if isinstance(windows, dict):
                    quota = {}
                    for name, w in windows.items():
                        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                            quota[name] = {
                                "utilization": float(w["utilization"]),
                                "resets_at": float(w.get("resetsAt") or 0) or None,
                            }
                    if quota:
                        res.quota = quota
            elif t == "system" and ev.get("subtype") == "api_retry":
                errors.append(f"api_retry: {ev.get('error')} (status {ev.get('error_status')})")
            elif t == "result":
                result_seen = True
                text = ev.get("result")
                res.summary = text if isinstance(text, str) else ""
                cost = ev.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    res.est_cost_usd = float(cost)
                usage = ev.get("usage") or {}
                if isinstance(usage, dict):
                    tin = sum(
                        int(usage.get(k) or 0)
                        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                    )
                    res.tokens_in = tin or None
                    res.tokens_out = int(usage.get("output_tokens") or 0) or None
                    res.tokens_cached = int(usage.get("cache_read_input_tokens") or 0) if tin else None
                if isinstance(ev.get("num_turns"), int):
                    res.model_calls = ev["num_turns"]
                if ev.get("is_error"):
                    errors.append(f"{ev.get('subtype')}: {res.summary[:500]}")
                    res.status = classify_text(res.summary) or RunStatus.TASK_FAILED
                else:
                    res.status = RunStatus.COMPLETED
        if rejected:
            res.status = RunStatus.RATE_LIMITED
        elif not result_seen:
            joined = "\n".join(errors + pr.tail[-20:])
            res.status = self.fallback_status(pr, joined)
            if res.status == RunStatus.COMPLETED:  # exit 0 but no result line: suspicious
                res.status = RunStatus.CRASHED
        res.error = "\n".join(errors)[-2000:]
        return res
