"""OpenAI Codex CLI (`codex exec --json`).

Event names follow the documented JSONL stream (thread.started, item.completed,
turn.completed, turn.failed, error). Verify against a real run with `orch probe codex`.
"""
from __future__ import annotations

from ..models import RunResult, RunStatus
from ..proc import ProcResult
from .base import Adapter, RunContext, classify_text, iter_json


class CodexAdapter(Adapter):
    executor = "codex"
    default_vendor = "openai"

    def build_cmd(self, exe: str, ctx: RunContext) -> list[str]:
        args = list(self.exec_cfg.get("args", []))
        if ctx.readonly:
            if "--sandbox" in args:
                args[args.index("--sandbox") + 1] = "read-only"
            else:
                args += ["--sandbox", "read-only"]
        cmd = [exe, "exec", *args]
        if self.model:
            cmd += ["-m", self.model]
        if ctx.resume_session:
            # Verified with codex-cli 0.160.1: exec-level flags (--json, --sandbox) go before `resume`.
            cmd += ["resume", ctx.resume_session]
        cmd.append(ctx.instruction)
        return cmd

    def parse(self, pr: ProcResult, ctx: RunContext) -> RunResult:
        res = RunResult(status=RunStatus.CRASHED)
        completed = failed = False
        errors: list[str] = []
        tin = tout = cached = 0
        actions = 0
        for ev in iter_json(pr.tail):
            t = str(ev.get("type", ""))
            if t == "thread.started" and ev.get("thread_id"):
                res.session_id = ev["thread_id"]
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    actions += 1
                if item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
                    res.summary = item["text"]
            elif t == "turn.completed":
                completed = True
                usage = ev.get("usage") or {}
                tin += int(usage.get("input_tokens") or 0)
                tout += int(usage.get("output_tokens") or 0)
                cached += int(usage.get("cached_input_tokens") or 0)
            elif t == "turn.failed":
                failed = True
                err = ev.get("error") or {}
                errors.append(str(err.get("message") if isinstance(err, dict) else err))
            elif t == "error":
                errors.append(str(ev.get("message") or ev))
        res.tokens_in = tin or None
        res.tokens_out = tout or None
        res.tokens_cached = cached if tin else None
        # Codex does not report its model calls; each tool action needs one, plus the final answer
        res.model_calls = actions + 1 if completed else None
        res.error = "\n".join(errors)[-2000:]
        if failed or (errors and not completed):
            # harness-level failure (not "the agent did the task badly"): infra -> handoff
            res.status = classify_text(res.error + "\n" + pr.stderr_tail) or RunStatus.CRASHED
        elif completed:
            res.status = RunStatus.COMPLETED
        else:
            res.status = self.fallback_status(pr, "\n".join(pr.tail[-20:]))
            if res.status == RunStatus.COMPLETED:
                res.status = RunStatus.CRASHED
        return res
