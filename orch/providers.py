"""Model providers a user can connect with their own account or API key.

Two ways to use a provider:
  coding  through a CLI that edits files and runs commands:
            Claude Code (`claude`, your Claude subscription), Codex (`codex`, your ChatGPT subscription),
            or opencode (`opencode`, any provider below with an API key)
  review  through the direct API executor (no tools): reviews and judging, a few thousand tokens each

Keys come from the provider's environment variable, or from `orch key set <provider>` (encrypted on
Windows). opencode gets the key of its own provider only; Claude Code and Codex never get API keys,
so they keep using your subscriptions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    base_url: str              # OpenAI-compatible chat API
    env: str                   # environment variable with the API key
    api_model: str             # default model for the direct API (reviews)
    opencode_model: str = ""   # provider/model for coding through opencode ("" = not offered)
    key_headers: tuple[str, ...] = ()
    signup: str = ""


PRESETS = [
    Preset("deepseek", "DeepSeek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-flash",
           "deepseek/deepseek-flash", signup="platform.deepseek.com"),
    Preset("openai", "GPT (OpenAI)", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5-mini",
           "openai/gpt-5-mini", signup="platform.openai.com"),
    Preset("anthropic", "Claude (Anthropic)", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", "claude-sonnet-5-5",
           "anthropic/claude-sonnet-5-5", key_headers=("x-api-key",), signup="console.anthropic.com"),
    Preset("qwen", "Qwen (Alibaba Cloud Bailian)", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           "DASHSCOPE_API_KEY", "qwen-plus", "alibaba-cn/qwen-plus", signup="bailian.console.aliyun.com"),
    Preset("gemini", "Gemini (Google)", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
           "gemini-2.5-flash", "google/gemini-2.5-flash", signup="aistudio.google.com"),
    Preset("doubao", "Doubao (Volcano Engine Ark)", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY",
           "", "", signup="console.volcengine.com/ark"),
]
BY_ID = {p.id: p for p in PRESETS}
# opencode's provider prefix -> preset (to hand opencode the right key)
BY_OPENCODE_PREFIX = {p.opencode_model.split("/")[0]: p for p in PRESETS if p.opencode_model}
BY_OPENCODE_PREFIX.update({"alibaba": BY_ID["qwen"], "google": BY_ID["gemini"]})


def key_for(preset: Preset, environ: dict | None = None) -> tuple[str | None, str]:
    """(key, source): source is env / saved / none. The key is never printed."""
    import os

    from .secrets import user_keys

    env = (environ if environ is not None else os.environ).get(preset.env, "").strip()
    if env:
        return env, "env"
    saved = user_keys().get(preset.id)
    return (saved, "saved") if saved else (None, "none")


def preset_for_base_url(base_url: str) -> Preset | None:
    base = base_url.rstrip("/")
    for p in PRESETS:
        if base == p.base_url.rstrip("/"):
            return p
    return None


def headers_for(preset: Preset | None, key: str) -> dict[str, str]:
    if preset is None:
        return {}
    h = {name: key for name in preset.key_headers}
    if preset.id == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    return h
