"""opencode (`opencode run --format json`) as the harness for API models (DeepSeek, ...).

Event shapes seen for real (opencode 1.18.35, Windows, deepseek/deepseek-flash):
  {"type":"step_start","sessionID":"ses_...","part":{"type":"step-start",...}}
  {"type":"tool_use","sessionID":...,"part":{"type":"tool","tool":"read","state":{"status":"completed",...}}}
  {"type":"step_finish","sessionID":...,"part":{"type":"step-finish","tokens":{"input":..,"output":..,
      "reasoning":..,"cache":{"read":..,"write":..}},"cost":0.0015609}}
  {"type":"text","sessionID":...,"part":{"type":"text","text":"..."}}
  {"type":"error","sessionID":...,"error":{"name":"UnknownError","data":{"message":...}}}
A failed tool call (part.state.status == "error") is normal agent work, not a run failure.
"""
from __future__ import annotations

from typing import Any

from ..models import RunResult, RunStatus
from ..proc import ProcResult
from .base import Adapter, RunContext, classify_text, iter_json

_SESSION_KEYS = ("sessionID", "sessionId", "session_id")


def _find_key(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k):
                return obj[k]
        for v in obj.values():
            found = _find_key(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, keys)
            if found:
                return found
    return None


class OpencodeAdapter(Adapter):
    executor = "opencode"

    @property
    def vendor(self) -> str:
        # opencode models are "provider/model" (deepseek/deepseek-flash -> deepseek)
        if self.pool_cfg.get("vendor"):
            return str(self.pool_cfg["vendor"]).lower()
        return (self.model.split("/", 1)[0] if "/" in self.model else self.pool).lower()

    def extra_env(self) -> dict[str, str]:
        """The key saved with `orch key set` for this pool's provider (only that one, only for opencode)."""
        from ..providers import BY_OPENCODE_PREFIX, key_for

        preset = BY_OPENCODE_PREFIX.get(self.model.split("/", 1)[0]) if "/" in self.model else None
        if preset is None:
            return {}
        key, source = key_for(preset)
        return {preset.env: key} if key and source == "saved" else {}

    def build_cmd(self, exe: str, ctx: RunContext) -> list[str]:
        cmd = [exe, "run", *self.exec_cfg.get("args", [])]
        if ctx.readonly:
            cmd += ["--agent", "plan"]  # opencode's built-in read-only agent
        if self.model:
            cmd += ["-m", self.model]
        if ctx.resume_session:
            cmd += ["--session", ctx.resume_session]
        cmd.append(ctx.instruction)
        return cmd

    def parse(self, pr: ProcResult, ctx: RunContext) -> RunResult:
        res = RunResult(status=RunStatus.CRASHED)
        errors: list[str] = []
        texts: list[str] = []
        cost = 0.0
        for ev in iter_json(pr.tail):
            sid = _find_key(ev, _SESSION_KEYS)
            if sid and isinstance(sid, str):
                res.session_id = sid
            t = str(ev.get("type", ""))
            part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
            if t == "error" or "error" in t:
                errors.append(str(ev.get("error") or ev.get("message") or ev)[:1000])
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            c = part.get("cost")
            if isinstance(c, (int, float)):
                cost += float(c)
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                # opencode reports cache reads/writes apart from "input"; tokens_in counts all of them,
                # the same way as the other executors, and tokens_cached the part read from cache
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                read, write = int(cache.get("read") or 0), int(cache.get("write") or 0)
                res.tokens_in = (res.tokens_in or 0) + int(tokens.get("input") or 0) + read + write
                res.tokens_cached = (res.tokens_cached or 0) + read
                res.tokens_out = (res.tokens_out or 0) + int(tokens.get("output") or 0)
            if t == "step_finish" or part.get("type") == "step-finish":
                res.model_calls = (res.model_calls or 0) + 1
        res.summary = (texts[-1] if texts else "\n".join(pr.tail[-5:]))[-4000:]
        res.est_cost_usd = cost or None
        res.error = "\n".join(errors)[-2000:]
        if errors:
            res.status = classify_text(res.error + "\n" + pr.stderr_tail) or RunStatus.CRASHED
        else:
            res.status = self.fallback_status(pr, "\n".join(pr.tail[-20:]))
        return res
