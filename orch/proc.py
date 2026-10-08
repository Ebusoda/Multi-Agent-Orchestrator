"""Cross-platform child-process helpers (Windows native + POSIX).

Notes on Windows:
- npm-installed CLIs are `.cmd` shims. We resolve them with shutil.which and never put
  free text (the prompt) on the command line, so cmd.exe quoting rules never matter.
- os.kill(pid, 0) TERMINATES a process on Windows, so pid_alive() uses the Win32 API.
- Children get their own process group so Ctrl+C in the orchestrator console does not
  hit them directly; we stop them ourselves (CTRL_BREAK first, then taskkill /T /F).
"""
from __future__ import annotations

import collections
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"
GRACE_SECONDS = 10

# Set by the main thread on Ctrl+C in parallel mode: worker threads never receive
# KeyboardInterrupt themselves, so every wait loop below also watches this event.
STOP = threading.Event()


class Slots:
    """How many runs each pool may have at the same time (pool `max_concurrent`, default 1)."""

    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = dict(limits or {})
        self.used: collections.Counter[str] = collections.Counter()
        self.version = 0  # bumped on every release, so the scheduler knows when to look again
        self._cond = threading.Condition()

    def limit(self, pool: str) -> int:
        return max(1, int(self.limits.get(pool, 1) or 1))

    def free(self, pool: str) -> bool:
        with self._cond:
            return self.used[pool] < self.limit(pool)

    def acquire(self, pool: str, on_wait=None) -> bool:
        """Blocks until the pool has room. Returns True if it had to wait. Raises
        KeyboardInterrupt if STOP is set while waiting."""
        waited = False
        with self._cond:
            while self.used[pool] >= self.limit(pool):
                if STOP.is_set():
                    raise KeyboardInterrupt
                if not waited and on_wait:
                    on_wait()
                waited = True
                self._cond.wait(timeout=1.0)
            self.used[pool] += 1
        return waited

    def release(self, pool: str) -> None:
        with self._cond:
            self.used[pool] = max(0, self.used[pool] - 1)
            self.version += 1
            self._cond.notify_all()


def resolve_command(name: str) -> str | None:
    """Full path of an executable on PATH (handles .exe/.cmd via PATHEXT on Windows)."""
    if not name:
        return None
    p = Path(name)
    if p.is_absolute() and p.exists():
        return str(p)
    return shutil.which(name)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")  # keep __pycache__ out of checkpoint commits
    if extra:
        env.update(extra)
    return env


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def stop_process(proc: subprocess.Popen, grace: float = GRACE_SECONDS) -> None:
    """Ask the child (and its tree) to stop, then force it."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    kill_tree(proc.pid)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


@dataclass
class ProcResult:
    exit_code: int | None
    timed_out: bool = False
    not_found: bool = False
    interrupted: bool = False
    tail: list[str] = field(default_factory=list)   # last stdout lines
    stderr_tail: str = ""
    pid: int | None = None


def run_streaming(
    cmd: list[str],
    cwd: Path,
    events_path: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
    on_start=None,
    tail_lines: int = 4000,
    stdin_text: str | None = None,
) -> ProcResult:
    """Run cmd, append every stdout line to events_path (UTF-8), stderr to *.stderr.log."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = events_path.with_suffix(".stderr.log")
    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
    if STOP.is_set():
        return ProcResult(exit_code=None, interrupted=True)

    try:
        with stderr_path.open("wb") as err_f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err_f,
                # PWD too: a shell that started orch (Git Bash) exports its own PWD, and some CLIs
                # (opencode) trust it over the real working directory and work in the wrong folder
                env=child_env({**(env or {}), "PWD": str(cwd)}),
                **popen_kwargs(),
            )
            if on_start:
                on_start(proc.pid)
            if stdin_text is not None:
                def feed() -> None:  # in a thread: a full pipe must never block the main loop
                    assert proc.stdin is not None
                    try:
                        proc.stdin.write(stdin_text.encode("utf-8"))
                        proc.stdin.close()
                    except OSError:
                        pass

                threading.Thread(target=feed, daemon=True).start()

            def pump() -> None:
                assert proc.stdout is not None
                with events_path.open("ab") as out_f:
                    for raw in proc.stdout:
                        out_f.write(raw)
                        out_f.flush()
                        tail.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            timed_out = interrupted = False
            deadline = time.monotonic() + timeout_s
            try:
                while True:
                    try:
                        proc.wait(timeout=1.0)
                        break
                    except subprocess.TimeoutExpired:
                        if STOP.is_set():
                            interrupted = True
                            stop_process(proc)
                            break
                        if time.monotonic() > deadline:
                            timed_out = True
                            stop_process(proc)
                            break
            except KeyboardInterrupt:
                interrupted = True
                stop_process(proc)
            reader.join(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            result = ProcResult(
                exit_code=proc.returncode,
                timed_out=timed_out,
                interrupted=interrupted,
                tail=list(tail),
                pid=proc.pid,
            )
    except FileNotFoundError:
        return ProcResult(exit_code=None, not_found=True)

    try:
        data = stderr_path.read_bytes()
        result.stderr_tail = data[-8000:].decode("utf-8", errors="replace")
    except OSError:
        pass
    return result


def run_capture(cmd: list[str], cwd: Path | None = None, timeout: float = 60) -> tuple[int | None, str]:
    """Short helper for `--version` style calls."""
    exe = resolve_command(cmd[0])
    if not exe:
        return None, "not found"
    try:
        p = subprocess.run(
            [exe, *cmd[1:]],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace").strip()
    return p.returncode, out
