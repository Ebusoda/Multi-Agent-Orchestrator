"""v0.3: minimal client for OpenAI-compatible chat APIs (DeepSeek, and others that speak the same
protocol). Standard library only (urllib); no SDK, no third-party gateway.

The API key is read from an environment variable named in the pool config, never from a file,
and is never written to logs, events or the database.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    def __init__(self, message: str, kind: str = "crashed", status: int | None = None):
        super().__init__(message)
        self.kind = kind          # auth | rate_limit | timeout | crashed
        self.status = status


@dataclass
class Reply:
    text: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_hit: int | None = None
    raw_usage: dict[str, Any] = field(default_factory=dict)


def api_key(env_name: str) -> str | None:
    value = os.environ.get(env_name or "", "").strip()
    return value or None


def _request(url: str, key: str, body: dict | None, timeout: float, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in (401, 403):
            raise LLMError(f"HTTP {e.code}: {detail}", "auth", e.code) from None
        if e.code in (402, 429):  # 402 = out of balance (DeepSeek), 429 = rate limit
            raise LLMError(f"HTTP {e.code}: {detail}", "rate_limit", e.code) from None
        raise LLMError(f"HTTP {e.code}: {detail}", "crashed", e.code) from None
    except (socket.timeout, TimeoutError):
        raise LLMError(f"no answer within {timeout:.0f}s", "timeout") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise LLMError(f"no answer within {timeout:.0f}s", "timeout") from None
        raise LLMError(f"cannot reach {url}: {e.reason}", "crashed") from None
    except json.JSONDecodeError as e:
        raise LLMError(f"answer is not JSON: {e}", "crashed") from None


def chat(base_url: str, key: str, model: str, messages: list[dict[str, str]], timeout: float = 300,
         max_tokens: int | None = None, temperature: float | None = None, headers: dict | None = None) -> Reply:
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    data = _request(base_url.rstrip("/") + "/chat/completions", key, body, timeout, headers)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected answer: {json.dumps(data)[:300]}") from None
    usage = data.get("usage") or {}
    # DeepSeek has reported cache hits both ways: its own field, and OpenAI's prompt_tokens_details
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return Reply(text=text, model=str(data.get("model") or model),
                 tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"),
                 cache_hit=hit, raw_usage=usage)


def list_models(base_url: str, key: str, timeout: float = 30, headers: dict | None = None) -> list[str]:
    data = _request(base_url.rstrip("/") + "/models", key, None, timeout, headers)
    return sorted(str(m.get("id")) for m in data.get("data") or [] if isinstance(m, dict))


def cost_usd(reply: Reply, prices: dict[str, Any]) -> float | None:
    """Dollars from the pool's prices per million tokens; None when no price is configured."""
    p_in = float(prices.get("price_in_per_m") or 0)
    p_out = float(prices.get("price_out_per_m") or 0)
    if not (p_in or p_out) or reply.tokens_in is None:
        return None
    p_hit = float(prices.get("price_cache_hit_per_m") or p_in)
    hit = reply.cache_hit or 0
    return ((reply.tokens_in - hit) * p_in + hit * p_hit + (reply.tokens_out or 0) * p_out) / 1_000_000


def _http_error(e: urllib.error.HTTPError) -> LLMError:
    detail = e.read().decode("utf-8", errors="replace")[:500]
    if e.code in (401, 403):
        return LLMError(f"HTTP {e.code}: {detail}", "auth", e.code)
    if e.code in (402, 429):
        return LLMError(f"HTTP {e.code}: {detail}", "rate_limit", e.code)
    return LLMError(f"HTTP {e.code}: {detail}", "crashed", e.code)


def chat_stream(base_url: str, key: str, model: str, messages: list[dict[str, str]], timeout: float = 300,
                max_tokens: int | None = None, headers: dict | None = None):
    """Streaming chat (server-sent events). Yields ("delta", text) pieces, then one ("done", Reply)."""
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                            "stream_options": {"include_usage": True}}
    if max_tokens:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    parts: list[str] = []
    usage: dict[str, Any] = {}
    seen_model = model
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                seen_model = str(ev.get("model") or seen_model)
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    text = (ch.get("delta") or {}).get("content")
                    if text:
                        parts.append(text)
                        yield "delta", text
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except (socket.timeout, TimeoutError):
        raise LLMError(f"no answer within {timeout:.0f}s", "timeout") from None
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {base_url}: {e.reason}", "crashed") from None
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    yield "done", Reply(text="".join(parts), model=seen_model, tokens_in=usage.get("prompt_tokens"),
                        tokens_out=usage.get("completion_tokens"), cache_hit=hit, raw_usage=usage)
