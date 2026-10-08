"""v0.3: direct API executor for steps that need no tools (review, judging).

A CLI agent explores the repository on its own and carries its harness's system prompt and tool
definitions on every call. A review only needs the specification and the diff, so this executor
sends exactly the files the prompt names, inline, in one request.

It cannot edit files or run commands, so the router never gives it coding or planning work
(`Adapter.tool_free`). Without the API key in the environment the pool is simply unavailable and
the router moves on to the next pool: nothing breaks when the key is missing.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import llm
from ..models import RunResult, RunStatus
from .base import Adapter, RunContext

DEFAULTS = {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-flash"}
MAX_INLINE_CHARS = 300_000
_PROMPT_FILE = re.compile(r"\.task/[A-Za-z0-9_.-]+\.md")
_REFERENCED = re.compile(r"`(\.task/[A-Za-z0-9_.-]+)`")
SYSTEM = (
    "You are called through an API by an orchestrator. You cannot open files, browse, or run "
    "commands: every file you need is included in the message, each under a '### File:' heading. "
    "Where the instructions say to open or check something, use the included files. Do not ask "
    "for more information; answer in the format the instructions require."
)

_STATUS = {"auth": RunStatus.AUTH_ERROR, "rate_limit": RunStatus.RATE_LIMITED,
           "timeout": RunStatus.TIMEOUT, "crashed": RunStatus.CRASHED}


class ApiAdapter(Adapter):
    executor = "api"
    tool_free = True  # cannot edit files or run commands

    def _cfg(self, key: str) -> str:
        return str(self.pool_cfg.get(key) or DEFAULTS[key])

    @property
    def model(self) -> str:
        return self._cfg("model")

    @property
    def vendor(self) -> str:
        if self.pool_cfg.get("vendor"):
            return str(self.pool_cfg["vendor"]).lower()
        host = urlparse(self._cfg("base_url")).hostname or self.pool
        parts = host.split(".")
        return (parts[-2] if len(parts) >= 2 else parts[0]).lower()   # api.deepseek.com -> deepseek

    def _key(self) -> str | None:
        """The pool's environment variable first, then the key saved with `orch key set`."""
        key = llm.api_key(self._cfg("api_key_env"))
        if key:
            return key
        from ..providers import BY_ID, preset_for_base_url
        from ..secrets import user_keys

        preset = BY_ID.get(str(self.pool_cfg.get("provider") or "")) or preset_for_base_url(self._cfg("base_url"))
        return user_keys().get(preset.id) if preset else None

    def _headers(self, key: str) -> dict[str, str]:
        from ..providers import BY_ID, headers_for, preset_for_base_url

        preset = BY_ID.get(str(self.pool_cfg.get("provider") or "")) or preset_for_base_url(self._cfg("base_url"))
        return headers_for(preset, key)

    def available(self) -> bool:
        return self._key() is not None

    def supports_resume(self) -> bool:
        return False

    def build_message(self, ctx: RunContext) -> tuple[str, list[str]]:
        """The prompt file the instruction names, followed by every .task/ file it references."""
        m = _PROMPT_FILE.search(ctx.instruction)
        prompt_rel = m.group(0) if m else ".task/PROMPT.md"
        prompt = (ctx.worktree / prompt_rel).read_text(encoding="utf-8-sig", errors="replace")
        parts, included, used = [prompt], [prompt_rel], len(prompt)
        from ..handoff import INLINE_MARK

        refs = [] if INLINE_MARK in prompt else _REFERENCED.findall(prompt)  # already copied in
        for rel in dict.fromkeys(refs):
            p = ctx.worktree / rel
            if rel == prompt_rel or not p.is_file():
                continue
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            room = MAX_INLINE_CHARS - used
            if room <= 0:
                break
            if len(text) > room:
                text = text[:room] + "\n[truncated by the orchestrator]\n"
            parts.append(f"\n### File: {rel}\n```\n{text}\n```")
            included.append(rel)
            used += len(text)
        return "\n".join(parts), included

    def run(self, ctx: RunContext) -> RunResult:
        key = self._key()
        if not key:
            return RunResult(status=RunStatus.UNAVAILABLE,
                             error=f"no API key: set {self._cfg('api_key_env')} or run orch key set")
        if not ctx.readonly:
            return RunResult(status=RunStatus.UNAVAILABLE, error="the api executor cannot edit files")
        try:
            message, included = self.build_message(ctx)
        except OSError as e:
            return RunResult(status=RunStatus.CRASHED, error=f"cannot read prompt: {e}")
        ctx.events_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        with open(ctx.events_path, "w", encoding="utf-8") as ev:
            ev.write(json.dumps({"type": "request", "model": self.model, "base_url": self._cfg("base_url"),
                                 "files": included, "chars": len(message)}, ensure_ascii=False) + "\n")
            try:
                reply = llm.chat(self._cfg("base_url"), key, self.model,
                                 [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}],
                                 timeout=ctx.timeout_s,
                                 max_tokens=int(self.pool_cfg.get("max_tokens") or 0) or None,
                                 headers=self._headers(key))
            except llm.LLMError as e:
                ev.write(json.dumps({"type": "error", "kind": e.kind, "error": str(e)}, ensure_ascii=False) + "\n")
                return RunResult(status=_STATUS.get(e.kind, RunStatus.CRASHED), error=str(e),
                                 events_path=ctx.events_path)
            ev.write(json.dumps({"type": "result", "model": reply.model, "usage": reply.raw_usage,
                                 "seconds": round(time.time() - start, 1), "text": reply.text},
                                ensure_ascii=False) + "\n")
        return RunResult(status=RunStatus.COMPLETED, summary=reply.text, tokens_in=reply.tokens_in,
                         tokens_cached=reply.cache_hit, model_calls=1,
                         tokens_out=reply.tokens_out, est_cost_usd=llm.cost_usd(reply, self.pool_cfg),
                         events_path=Path(ctx.events_path), exit_code=0)
