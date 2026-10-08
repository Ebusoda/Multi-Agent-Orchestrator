"""Claude Code and Codex sessions on this computer, including the ones you open by hand.

Read-only. Both CLIs keep one JSONL log per session:
- Claude Code: <CLAUDE_CONFIG_DIR or ~/.claude>/projects/<folder>/<session id>.jsonl
- Codex:       <CODEX_HOME or ~/.codex>/sessions/YYYY/MM/DD/rollout-*.jsonl

Only these fields are kept: times, the project folder, git branch, model, token counts and (Codex) the
subscription windows. Conversation content is never kept, printed or sent anywhere, and nothing else
in those folders is opened: no settings, no history file, no login file.

A session counts as active when its log was written in the last ACTIVE_MINUTES minutes. Sessions that
orch itself started run inside its worktrees (`<project>.worktrees/`) and are marked source="orch".
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

ACTIVE_MINUTES = 5
WINDOWS = {300: "five_hour", 10080: "seven_day"}  # Codex reports window lengths in minutes


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _source(cwd: str) -> str:
    parts = (cwd or "").replace("\\", "/").split("/")  # a Windows path, also when read on another system
    return "orch" if any(part.endswith(".worktrees") for part in parts) else "manual"


def _lines(path: Path):
    """JSON objects of a log, one per line; unreadable lines are skipped."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _session(tool: str, sid: str, path: Path) -> dict:
    return {"tool": tool, "id": sid, "project": "", "branch": "", "model": "", "source": "manual",
            "started_at": None, "last_at": None, "active": False, "turns": 0,
            "tokens": {"in": 0, "out": 0, "cached": 0}, "tokens_today": 0, "log": str(path)}


def _claude_session(path: Path, midnight: float) -> dict:
    s = _session("claude_code", path.stem, path)
    seen: set[str] = set()
    for rec in _lines(path):
        ts = _ts(rec.get("timestamp"))
        if ts:
            s["started_at"] = s["started_at"] or ts
            s["last_at"] = ts
        if rec.get("cwd") and not s["project"]:
            s["project"] = str(rec["cwd"])
        if rec.get("gitBranch"):
            s["branch"] = str(rec["gitBranch"])
        msg = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        mid = str(msg.get("id") or rec.get("requestId") or rec.get("uuid") or "")
        if not isinstance(usage, dict) or mid in seen:  # one reply is logged once per content block
            continue
        seen.add(mid)
        s["model"] = str(msg.get("model") or s["model"])
        s["turns"] += 1
        tin = sum(int(usage.get(k) or 0) for k in
                  ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out = int(usage.get("output_tokens") or 0)
        s["tokens"]["in"] += tin
        s["tokens"]["out"] += out
        s["tokens"]["cached"] += int(usage.get("cache_read_input_tokens") or 0)
        if ts and ts >= midnight:
            s["tokens_today"] += tin + out
    return s


def _codex_session(path: Path, midnight: float, quota: dict) -> dict:
    s = _session("codex", path.stem, path)
    before_today = 0
    for rec in _lines(path):
        ts = _ts(rec.get("timestamp"))
        if ts:
            s["started_at"] = s["started_at"] or ts
            s["last_at"] = ts
        p = rec.get("payload")
        if not isinstance(p, dict):
            continue
        kind = rec.get("type")
        if kind == "session_meta":
            s["id"] = str(p.get("id") or s["id"])
            s["project"] = str(p.get("cwd") or s["project"])
            s["started_at"] = _ts(p.get("timestamp")) or s["started_at"]
            git = p.get("git")
            if isinstance(git, dict) and git.get("branch"):
                s["branch"] = str(git["branch"])
        elif kind == "turn_context":
            s["model"] = str(p.get("model") or s["model"])
            s["project"] = s["project"] or str(p.get("cwd") or "")
        elif kind == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            total = info.get("total_token_usage")
            if isinstance(total, dict):  # running totals for the session
                tin, out = int(total.get("input_tokens") or 0), int(total.get("output_tokens") or 0)
                s["tokens"] = {"in": tin, "out": out, "cached": int(total.get("cached_input_tokens") or 0)}
                s["turns"] += 1
                if ts and ts < midnight:
                    before_today = tin + out
            rl = p.get("rate_limits")
            if isinstance(rl, dict) and ts and ts >= quota.get("_at", 0):
                windows = {}
                for w in rl.values():
                    if not isinstance(w, dict) or "used_percent" not in w:
                        continue
                    name = WINDOWS.get(int(w.get("window_minutes") or 0))
                    resets = _ts(w.get("resets_at")) or (ts + float(w["resets_in_seconds"])
                                                          if w.get("resets_in_seconds") is not None else None)
                    if name:
                        windows[name] = {"utilization": round(float(w["used_percent"]) / 100, 4),
                                         "resets_at": resets}
                if windows:
                    quota.clear()
                    quota.update(windows, _at=ts)
    s["tokens_today"] = max(0, s["tokens"]["in"] + s["tokens"]["out"] - before_today)
    return s


def scan(days: float = 7, now: float | None = None) -> dict:
    """Sessions whose log changed in the last `days` days, newest first, with a summary per tool."""
    now = now or time.time()
    since = now - days * 86400
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    sessions: list[dict] = []
    quota: dict = {}
    logs = [("claude_code", p) for p in (claude_dir() / "projects").glob("*/*.jsonl")]
    logs += [("codex", p) for p in (codex_dir() / "sessions").glob("*/*/*/rollout-*.jsonl")]
    for tool, path in logs:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        s = _claude_session(path, midnight) if tool == "claude_code" else _codex_session(path, midnight, quota)
        s["source"] = _source(s["project"])
        s["last_at"] = max(s["last_at"] or 0, mtime)
        s["active"] = now - s["last_at"] < ACTIVE_MINUTES * 60
        sessions.append(s)
    sessions.sort(key=lambda s: s["last_at"] or 0, reverse=True)
    summary: dict[str, dict] = {}
    for s in sessions:
        t = summary.setdefault(s["tool"], {"sessions": 0, "manual": 0, "active": 0, "tokens_today": 0,
                                           "tokens_today_manual": 0})
        t["sessions"] += 1
        t["active"] += s["active"]
        t["tokens_today"] += s["tokens_today"]
        if s["source"] == "manual":
            t["manual"] += 1
            t["tokens_today_manual"] += s["tokens_today"]
    quota.pop("_at", None)
    live = {k: w for k, w in quota.items() if (w.get("resets_at") or 0) > now}
    return {"at": now, "days": days, "sessions": sessions, "summary": summary,
            "codex_quota": live or None,
            "dirs": {"claude_code": str(claude_dir() / "projects"), "codex": str(codex_dir() / "sessions")}}
