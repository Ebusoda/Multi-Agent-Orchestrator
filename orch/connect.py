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
from .i18n import tr
from .tomledit import add_pool, set_route

CLIS = [("claude", "Claude Code", "your Claude subscription", "npm install -g @anthropic-ai/claude-code; then run `claude` once to sign in"),
        ("codex", "Codex CLI", "your ChatGPT subscription", "npm install -g @openai/codex; then run `codex` once to sign in"),
        ("opencode", "opencode", "any API provider below", "npm install -g opencode-ai")]   # translated where shown


def _read_key(prompt: str) -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip() if sys.stdin else ""   # piped in (scripts): never wait on a console


def key_set(provider: str, value: str | None = None, read=_read_key) -> str:
    if provider not in BY_ID:
        raise ValueError(tr("unknown provider {0!r}; one of: {1}", provider, ", ".join(BY_ID)))
    value = value if value is not None else read(tr("{0} API key (input hidden): ", BY_ID[provider].name))
    if not value.strip():
        raise ValueError(tr("empty key, nothing saved"))
    user_keys().set(provider, value)
    return tr("saved the {0} key (encrypted for this Windows account)", BY_ID[provider].name) if sys.platform == "win32" \
        else tr("saved the {0} key", BY_ID[provider].name)


def key_remove(provider: str) -> str:
    if provider not in BY_ID:
        raise ValueError(tr("unknown provider {0!r}", provider))
    user_keys().set(provider, "")
    return tr("removed the saved {0} key", BY_ID[provider].name)


def key_list() -> list[str]:
    out = []
    for p in PRESETS:
        _, source = key_for(p)
        where = {"env": tr("set (environment variable {0})", p.env), "saved": tr("set (saved with orch key set)"),
                 "none": tr("not set   ->  orch key set {0}   (get one at {1})", p.id, p.signup)}[source]
        out.append(f"{p.id:<10} {p.name:<30} {where}")
    return out


def status(cfg: Config) -> list[str]:
    lines = [tr("Coding agents (they edit files and run commands in a task's own worktree):")]
    for cmd, name, account, install in CLIS:
        path = resolve_command(cfg.executor_cfg({"claude": "claude_code"}.get(cmd, cmd)).get("command", cmd))
        lines.append(tr("  {0:<12} {1:<14} uses {2}", name, tr("installed") if path else tr("not installed"), tr(account))
                     + ("" if path else tr("\n               install: {0}", install)))
    lines += ["", tr("API providers (coding through opencode, reviews through the direct API):")]
    for p in PRESETS:
        _, source = key_for(p)
        pools = [name for name, pc in cfg.pools.items()
                 if pc.get("provider") == p.id or (p.opencode_model and pc.get("model") == p.opencode_model)
                 or pc.get("base_url", "").rstrip("/") == p.base_url.rstrip("/")]
        used = tr("pools: {0}", ", ".join(pools)) if pools else tr("not used yet  ->  orch connect {0}", p.id)
        lines.append(tr("  {0:<10} key {1:<8} {2}", p.id, tr("set") if source != "none" else tr("missing"), used))
    lines += ["", tr("Keys: orch key set <provider> (or the provider's environment variable). opencode can also keep"),
              tr("its own login (opencode auth login); a pool works with either.")]
    return lines


def connect(cfg: Config, provider: str, purpose: str = "coding", model: str = "") -> list[str]:
    """Add a pool for the provider to this project's orch.toml and put it first where it fits."""
    if provider not in BY_ID:
        raise ValueError(tr("unknown provider {0!r}; one of: {1}", provider, ", ".join(BY_ID)))
    p = BY_ID[provider]
    path = cfg.agents_dir / "orch.toml"
    if not path.is_file():
        raise ValueError(tr("run `orch init` in this project first"))
    _, source = key_for(p)
    out = []
    if purpose == "coding":
        if not (model or p.opencode_model):
            raise ValueError(tr("{0} has no default coding model; pass --model provider/model (see `opencode models`)", p.name))
        if not resolve_command(cfg.executor_cfg("opencode").get("command", "opencode")):
            raise ValueError(tr("coding through an API provider needs opencode: npm install -g opencode-ai"))
        name = provider
        added = add_pool(path, name, {"executor": "opencode", "model": model or p.opencode_model, "max_concurrent": 2},
                 f"{p.name} through opencode (added by orch connect)")
        routing = Config.load(cfg.project).data["routing"]
        for key in ("code_change.S", "code_change.M"):
            current = cfg.route_for("code_change", key[-1])
            set_route(path, routing, key, [name] + [x for x in current if x != name])
            routing = Config.load(cfg.project).data["routing"]
        out.append(tr("added pool `{0}` (opencode); small and medium coding tasks now start on it", name) if added
                   else tr("pool `{0}` (opencode); small and medium coding tasks now start on it", name))
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
        out.append(tr("added pool `{0}` (direct API); it reviews and judges first (work by the same company is still "
                      "reviewed by another one)", name) if added else
                   tr("pool `{0}` (direct API); it reviews and judges first (work by the same company is still "
                      "reviewed by another one)", name))
    else:
        raise ValueError(tr("--for is coding or review"))
    if source == "none":
        out.append(tr("no key yet: run `orch key set {0}` (or set {1})", provider, p.env))
    out.append(tr("check it: {0}", f"orch api check {name}" if purpose == "review" else "orch doctor"))
    return out
