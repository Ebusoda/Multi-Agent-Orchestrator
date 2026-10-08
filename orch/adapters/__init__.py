"""Executor registry: executor name in orch.toml -> adapter class."""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from .api import ApiAdapter
from .base import Adapter, RunContext
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .fake import FakeAdapter
from .opencode import OpencodeAdapter

REGISTRY: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "opencode": OpencodeAdapter,
    "fake": FakeAdapter,
    "api": ApiAdapter,
}


def make_adapter(cfg: Config, pool: str) -> Adapter:
    pool_cfg = cfg.pools.get(pool)
    if pool_cfg is None:
        raise KeyError(f"pool '{pool}' is not defined in [pools]")
    executor = pool_cfg.get("executor", "")
    cls = REGISTRY.get(executor)
    if cls is None:
        raise KeyError(f"pool '{pool}': unknown executor '{executor}' (known: {', '.join(REGISTRY)})")
    return cls(pool, pool_cfg, cfg.executor_cfg(executor), Path(cfg.project))


__all__ = ["Adapter", "RunContext", "make_adapter", "REGISTRY"]
