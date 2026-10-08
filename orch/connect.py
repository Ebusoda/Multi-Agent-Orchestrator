"""`orch key` and `orch connect`: use your own model accounts from the command line.

    orch key set deepseek            paste a key (hidden); encrypted on Windows, never printed
    orch key list                    which keys are set, and where from (environment / saved)
    orch connect                     what can be used now: subscription CLIs and API providers
    orch connect deepseek            code with DeepSeek through opencode (first in the S and M routes)
    orch connect openai --for review GPT reviews through the direct API
"""
from __future__ import annotations

import getpass
import sys

from .config import Config
from .proc import resolve_command
from .providers import BY_ID, PRESETS, key_for
from .secrets import user_keys
from .tomledit import add_pool, set_route

CLIS = [("claude", "Claude Code", "your Claude subscription", "npm install -g @anthropic-ai/claude-code; then run `claude` once to sign in"),
        ("codex", "Codex CLI", "your ChatGPT subscription", "npm install -g @openai/codex; then run `codex` once to sign in"),
        ("opencode", "opencode", "any API provider below", "npm install -g opencode-ai")]


def _read_key(prompt: str) -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip() if sys.stdin else ""   # piped in (scripts): never wait on a console


def key_set(provider: str, value: str | None = None, read=_read_key) -> str:
    if provider not in BY_ID:
        raise ValueError(f"unknown provider {provider!r}; one of: {', '.join(BY_ID)}")
    value = value if value is not None else read(f"{BY_ID[provider].name} API key (input hidden): ")
    if not value.strip():
        raise ValueError("empty key, nothing saved")
    user_keys().set(provider, value)
    return f"saved the {BY_ID[provider].name} key (encrypted for this Windows account)" if sys.platform == "win32" \
        else f"saved the {BY_ID[provider].name} key"


def key_remove(provider: str) -> str:
    if provider not in BY_ID:
        raise ValueError(f"unknown provider {provider!r}")
    user_keys().set(provider, "")
    return f"removed the saved {BY_ID[provider].name} key"


def key_list() -> list[str]:
    out = []
    for p in PRESETS:
        _, source = key_for(p)
        where = {"env": f"set (environment variable {p.env})", "saved": "set (saved with orch key set)",
                 "none": f"not set   ->  orch key set {p.id}   (get one at {p.signup})"}[source]
        out.append(f"{p.id:<10} {p.name:<30} {where}")
    return out


def status(cfg: Config) -> list[str]:
    lines = ["Coding agents (they edit files and run commands in a task's own worktree):"]
    for cmd, name, account, install in CLIS:
        path = resolve_command(cfg.executor_cfg({"claude": "claude_code"}.get(cmd, cmd)).get("command", cmd))
        lines.append(f"  {name:<12} {'installed' if path else 'not installed':<14} uses {account}"
                     + ("" if path else f"\n               install: {install}"))
    lines += ["", "API providers (coding through opencode, reviews through the direct API):"]
    for p in PRESETS:
        _, source = key_for(p)
        pools = [name for name, pc in cfg.pools.items()
                 if pc.get("provider") == p.id or (p.opencode_model and pc.get("model") == p.opencode_model)
                 or pc.get("base_url", "").rstrip("/") == p.base_url.rstrip("/")]
        used = f"pools: {', '.join(pools)}" if pools else f"not used yet  ->  orch connect {p.id}"
        lines.append(f"  {p.id:<10} key {'set' if source != 'none' else 'missing':<8} {used}")
    lines += ["", "Keys: orch key set <provider> (or the provider's environment variable). opencode can also keep",
              "its own login (opencode auth login); a pool works with either."]
    return lines


def connect(cfg: Config, provider: str, purpose: str = "coding", model: str = "") -> list[str]:
    """Add a pool for the provider to this project's orch.toml and put it first where it fits."""
    if provider not in BY_ID:
        raise ValueError(f"unknown provider {provider!r}; one of: {', '.join(BY_ID)}")
    p = BY_ID[provider]
    path = cfg.agents_dir / "orch.toml"
    if not path.is_file():
        raise ValueError("run `orch init` in this project first")
    _, source = key_for(p)
    out = []
    if purpose == "coding":
        if not (model or p.opencode_model):
            raise ValueError(f"{p.name} has no default coding model; pass --model provider/model (see `opencode models`)")
        if not resolve_command(cfg.executor_cfg("opencode").get("command", "opencode")):
            raise ValueError("coding through an API provider needs opencode: npm install -g opencode-ai")
        name = provider
        added = add_pool(path, name, {"executor": "opencode", "model": model or p.opencode_model, "max_concurrent": 2},
                 f"{p.name} through opencode (added by orch connect)")
        routing = Config.load(cfg.project).data["routing"]
        for key in ("code_change.S", "code_change.M"):
            current = cfg.route_for("code_change", key[-1])
            set_route(path, routing, key, [name] + [x for x in current if x != name])
            routing = Config.load(cfg.project).data["routing"]
        what = "added pool" if added else "pool"
        out.append(f"{what} `{name}` (opencode); small and medium coding tasks now start on it")
    elif purpose == "review":
        name = f"{provider}_api"
        added = add_pool(path, name, {"executor": "api", "provider": p.id, "base_url": p.base_url, "api_key_env": p.env,
                              "model": model or p.api_model}, f"{p.name}, direct API for reviews and judging")
        routing = Config.load(cfg.project).data["routing"]
        for role in ("review", "judge"):
            current = cfg.role_pools(role, "review" if role == "review" else "decide",
                                     "pools" if role == "review" else "judge")
            set_route(path, routing, role, [name] + [x for x in current if x != name])
            routing = Config.load(cfg.project).data["routing"]
        out.append(f"{'added pool' if added else 'pool'} `{name}` (direct API); it reviews and judges first "
                   "(work by the same company is still reviewed by another one)")
    else:
        raise ValueError("--for is coding or review")
    if source == "none":
        out.append(f"no key yet: run `orch key set {provider}` (or set {p.env})")
    out.append("check it: " + (f"orch api check {name}" if purpose == "review" else "orch doctor"))
    return out
