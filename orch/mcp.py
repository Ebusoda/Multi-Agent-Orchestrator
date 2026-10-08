"""MCP server: use orch from a chat app (Claude Desktop, ChatGPT and other MCP clients).

    python -m orch mcp --project F:\\work\\repo-a --project F:\\work\\repo-b

Speaks the Model Context Protocol over stdio (JSON-RPC 2.0, one message per line). The chat model
can look at projects and tasks, add tasks and start runs; orch itself still decides routing,
acceptance and merging, so the chat model never becomes the control centre. Runs start as separate
`python -m orch run` processes and keep going when the chat ends. High-risk tasks still wait for a
human merge, exactly as on the command line.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
ROOT = Path(__file__).resolve().parents[1]


class OrchTools:
    def __init__(self, projects: list[str], python: str | None = None):
        self.projects = {Path(p).resolve().name: Path(p).resolve() for p in projects}
        self.python = python or sys.executable
        self.running: dict[str, subprocess.Popen] = {}

    # -- helpers -------------------------------------------------------------------
    def _path(self, project: str) -> Path:
        if project not in self.projects:
            raise ValueError(f"unknown project {project!r}; known: {', '.join(self.projects) or '(none)'}")
        return self.projects[project]

    def _orch(self, project: str, *args: str, timeout: float = 120) -> str:
        p = subprocess.run([self.python, "-m", "orch", "-p", str(self._path(project)), *args],
                           cwd=str(ROOT), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (p.stdout + p.stderr).decode("utf-8", errors="replace").strip()
        if p.returncode != 0:
            raise ValueError(out[-2000:] or f"orch exited with {p.returncode}")
        return out[-8000:]

    def _db(self, project: str) -> sqlite3.Connection:
        db = self._path(project) / ".agents" / "state.db"
        if not db.is_file():
            raise ValueError(f"{project} is not an orch project yet (run: python -m orch -p <path> init)")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    # -- tools ---------------------------------------------------------------------
    def list_projects(self) -> dict:
        out = []
        for name in self.projects:
            try:
                con = self._db(name)
                counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
                con.close()
            except (ValueError, sqlite3.Error) as e:
                counts = {"error": str(e)}
            proc = self.running.get(name)
            out.append({"project": name, "path": str(self.projects[name]), "tasks": counts,
                        "run_in_progress": bool(proc and proc.poll() is None)})
        return {"projects": out}

    def list_tasks(self, project: str, status: str = "") -> dict:
        con = self._db(project)
        sql = "SELECT id, title, status, difficulty, risk, note FROM tasks" + (" WHERE status=?" if status else "")
        rows = [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT 50", (status,) if status else ())]
        con.close()
        return {"tasks": rows}

    def task_detail(self, project: str, task_id: str) -> dict:
        con = self._db(project)
        t = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            con.close()
            raise ValueError(f"no task {task_id}")
        runs = [dict(r) for r in con.execute(
            "SELECT id, pool, kind, status, verified, tokens_in, tokens_cached, model_calls, est_cost_usd "
            "FROM runs WHERE task_id=? ORDER BY id", (task_id,))]
        con.close()
        task = {k: t[k] for k in ("id", "title", "spec", "type", "difficulty", "risk", "status", "note", "branch")}
        task["verify"] = json.loads(t["verify"] or "[]")
        task["scope"] = json.loads(t["scope"] or "[]")
        return {"task": task, "runs": runs}

    def add_task(self, project: str, title: str, spec: str = "", difficulty: str = "M", risk: str = "normal",
                 verify: list[str] | None = None, scope: list[str] | None = None, task_type: str = "code_change") -> dict:
        args = ["task", "add", "--title", title, "--spec", spec, "--difficulty", difficulty, "--risk", risk,
                "--type", task_type]
        for v in verify or []:
            args += ["--verify", v]
        for s in scope or []:
            args += ["--scope", s]
        return {"output": self._orch(project, *args)}

    def run(self, project: str, task_ids: list[str] | None = None, auto_merge: bool = True) -> dict:
        proc = self.running.get(project)
        if proc and proc.poll() is None:
            return {"started": False, "reason": "a run is already in progress for this project", "pid": proc.pid}
        args = [self.python, "-m", "orch", "-p", str(self._path(project)), "run", *(task_ids or [])]
        if auto_merge:
            args.append("--auto-merge")
        log = self._path(project) / ".agents" / "mcp_run.log"
        with open(log, "ab") as f:
            proc = subprocess.Popen(args, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                    **_detached())
        self.running[project] = proc
        return {"started": True, "pid": proc.pid, "log": str(log),
                "note": "runs in the background; check progress with orch_list_tasks / orch_task"}

    def report(self, project: str) -> dict:
        return {"report": self._orch(project, "report")}

    def merge(self, project: str, task_id: str) -> dict:
        return {"output": self._orch(project, "merge", task_id, timeout=900)}

    def context(self, project: str, task_id: str) -> dict:
        return {"output": self._orch(project, "context", task_id)}


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


_P = {"project": {"type": "string", "description": "project name, as listed by orch_list_projects"}}
TOOLS: list[dict[str, Any]] = [
    {"name": "orch_list_projects", "description": "Projects this orch server manages, with task counts per status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "orch_list_tasks", "description": "Tasks of a project, newest first (optionally only one status: "
     "queued, running, verified, merged, blocked, failed, cancelled).",
     "inputSchema": {"type": "object", "properties": {**_P, "status": {"type": "string"}}, "required": ["project"]}},
    {"name": "orch_task", "description": "One task: its specification, status, note and every run (pool, tokens, cost).",
     "inputSchema": {"type": "object", "properties": {**_P, "task_id": {"type": "string"}},
                     "required": ["project", "task_id"]}},
    {"name": "orch_add_task", "description": "Queue a coding task. Give acceptance commands (verify) whenever "
     "possible: a task is only done when they exit with code 0.",
     "inputSchema": {"type": "object", "properties": {
         **_P, "title": {"type": "string"}, "spec": {"type": "string"},
         "difficulty": {"type": "string", "enum": ["S", "M", "L"]},
         "risk": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
         "verify": {"type": "array", "items": {"type": "string"}},
         "scope": {"type": "array", "items": {"type": "string"}, "description": "files the task may change"}},
         "required": ["project", "title"]}},
    {"name": "orch_run", "description": "Start running queued tasks in the background (orch picks models, verifies "
     "and merges; high-risk tasks still wait for a human merge).",
     "inputSchema": {"type": "object", "properties": {
         **_P, "task_ids": {"type": "array", "items": {"type": "string"}}, "auto_merge": {"type": "boolean"}},
         "required": ["project"]}},
    {"name": "orch_report", "description": "Cost and quality report: first-pass rate and cost per successful task "
     "by task type and pool.", "inputSchema": {"type": "object", "properties": {**_P}, "required": ["project"]}},
    {"name": "orch_context", "description": "What each model call of a task was given (context manifest).",
     "inputSchema": {"type": "object", "properties": {**_P, "task_id": {"type": "string"}},
                     "required": ["project", "task_id"]}},
    {"name": "orch_merge", "description": "Merge a verified task into the integration branch (the human approval "
     "step for high-risk tasks; only call it when the user asked for it).",
     "inputSchema": {"type": "object", "properties": {**_P, "task_id": {"type": "string"}},
                     "required": ["project", "task_id"]}},
]


class Server:
    def __init__(self, tools: OrchTools):
        self.tools = tools
        self.calls: dict[str, Callable[..., dict]] = {
            "orch_list_projects": lambda a: tools.list_projects(),
            "orch_list_tasks": lambda a: tools.list_tasks(a["project"], a.get("status", "")),
            "orch_task": lambda a: tools.task_detail(a["project"], a["task_id"]),
            "orch_add_task": lambda a: tools.add_task(a["project"], a["title"], a.get("spec", ""),
                                                      a.get("difficulty", "M"), a.get("risk", "normal"),
                                                      a.get("verify"), a.get("scope")),
            "orch_run": lambda a: tools.run(a["project"], a.get("task_ids"), a.get("auto_merge", True)),
            "orch_report": lambda a: tools.report(a["project"]),
            "orch_context": lambda a: tools.context(a["project"], a["task_id"]),
            "orch_merge": lambda a: tools.merge(a["project"], a["task_id"]),
        }

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification (e.g. notifications/initialized): no answer
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in SUPPORTED else PROTOCOL, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "orch", "version": "0.3"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                fn = self.calls.get(params.get("name", ""))
                if fn is None:
                    return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
                try:
                    data = fn(params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}]}
                except (ValueError, KeyError, subprocess.TimeoutExpired) as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never let one bad request kill the server
            return _error(mid, -32603, str(e))
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                out = _error(None, -32700, "parse error")
            else:
                out = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
            if out is not None:
                stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                stdout.flush()


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(projects: list[str]) -> int:
    # stdout carries the protocol; keep it UTF-8 and line-buffered
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    Server(OrchTools(projects)).serve()
    return 0
