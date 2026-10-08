"""Run a task's verify commands outside the agent. Exit codes decide 'done', not the agent."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .proc import STOP, child_env, kill_tree, popen_kwargs


def run_verify(commands: list[str], cwd: Path, log_path: Path, timeout_s: float) -> tuple[bool, str]:
    """Run each command through the platform shell. Returns (all_passed, log_text)."""
    lines: list[str] = []
    passed = True
    if not commands:
        lines.append("(no verify commands defined: nothing objective to check)")
    for cmd in commands:
        lines.append(f"$ {cmd}")
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env({"PWD": str(cwd)}),
                **popen_kwargs(),
            )
            deadline = start + timeout_s
            while True:
                try:
                    out, _ = proc.communicate(timeout=1.0)  # may be called again after a timeout
                    code = proc.returncode
                    break
                except subprocess.TimeoutExpired:
                    if STOP.is_set() or time.monotonic() > deadline:
                        kill_tree(proc.pid)
                        out, _ = proc.communicate()
                        code = None
                        break
            text = out.decode("utf-8", errors="replace")
        except OSError as e:
            text, code = str(e), None
        lines.append(text.rstrip())
        took = time.monotonic() - start
        lines.append(f"[exit {code if code is not None else 'TIMEOUT'} in {took:.1f}s]\n")
        if code != 0:
            passed = False
            break  # later commands usually depend on earlier ones
    lines.append("RESULT: PASS" if passed else "RESULT: FAIL")
    log = "\n".join(lines)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log, encoding="utf-8")
    return passed, log
