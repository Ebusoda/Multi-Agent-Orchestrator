"""Git plumbing: one worktree + branch per task, checkpoint commits, serial squash merge.

The user's own checkout of the project is never modified: integration happens in a
dedicated `_integration` worktree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .proc import child_env

# gc.auto=0: orch commits in several worktrees at once; an automatic gc must not race them.
NO_GC = ["-c", "gc.auto=0"]
# Last resort only (demo / test repos, or a machine with no git identity at all).
ORCH_IDENT = ["-c", "user.name=orch", "-c", "user.email=orch@localhost", *NO_GC]
_configured: tuple[str, str] | None = None   # [git] name / email from the project's orch.toml


def set_identity(name: str, email: str) -> None:
    """Who orch's own commits (checkpoints, merges, project memory) are made as. Empty = not set."""
    global _configured
    _configured = (name.strip(), email.strip()) if name.strip() and email.strip() else None


def ident(cwd: Path) -> list[str]:
    """Commit identity for orch's own commits: orch.toml [git] -> the project's git config -> orch."""
    if _configured:
        return ["-c", f"user.name={_configured[0]}", "-c", f"user.email={_configured[1]}", *NO_GC]
    if git(["config", "--get", "user.name"], cwd, check=False) and git(["config", "--get", "user.email"], cwd, check=False):
        return list(NO_GC)
    return list(ORCH_IDENT)
EXCLUDES = [".task/", ".agents/"]


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path, check: bool = True, timeout: float = 120) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
    )
    out = p.stdout.decode("utf-8", errors="replace")
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", errors="replace")
        raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {err.strip() or out.strip()}")
    return out.strip()


def is_repo(path: Path) -> bool:
    try:
        return git(["rev-parse", "--is-inside-work-tree"], path, check=True) == "true"
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def has_commits(path: Path) -> bool:
    try:
        git(["rev-parse", "--verify", "HEAD"], path)
        return True
    except GitError:
        return False


def current_branch(path: Path) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], path)


def branch_exists(repo: Path, branch: str) -> bool:
    return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False) != ""


def setup_excludes(repo: Path) -> None:
    """Keep orchestrator files out of every worktree's `git status` (shared info/exclude)."""
    common = Path(git(["rev-parse", "--git-common-dir"], repo))
    if not common.is_absolute():
        common = (repo / common).resolve()
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [e for e in EXCLUDES if e not in existing.splitlines()]
    if missing:
        with exclude.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# added by MAO orchestrator\n")
            for e in missing:
                f.write(e + "\n")


def ensure_branch(repo: Path, branch: str, start: str) -> None:
    if not branch_exists(repo, branch):
        git(["branch", branch, start], repo)


def _registered_worktrees(repo: Path) -> set[Path]:
    out = git(["worktree", "list", "--porcelain"], repo)
    return {Path(line[len("worktree "):]).resolve() for line in out.splitlines() if line.startswith("worktree ")}


def ensure_worktree(repo: Path, path: Path, branch: str, start: str) -> Path:
    """Create (or reuse) a worktree at `path` on `branch`, branching from `start` if new."""
    path = path.resolve()
    if path in _registered_worktrees(repo) and path.exists():
        return path
    git(["worktree", "prune"], repo, check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(repo, branch):
        git(["worktree", "add", str(path), branch], repo)
    else:
        git(["worktree", "add", "-b", branch, str(path), start], repo)
    return path


def ensure_detached_worktree(repo: Path, path: Path, ref: str) -> Path:
    """A throwaway worktree on a detached HEAD at `ref`, reset to it on every call."""
    path = path.resolve()
    if path in _registered_worktrees(repo) and path.exists():
        git(["checkout", "-q", "--detach", ref], path)
        git(["reset", "-q", "--hard", ref], path)
        git(["clean", "-fdq"], path, check=False)
        return path
    git(["worktree", "prune"], repo, check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", str(path), ref], repo)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    git(["worktree", "remove", "--force", str(path)], repo, check=False)
    git(["worktree", "prune"], repo, check=False)


def dirty(wt: Path) -> bool:
    return git(["status", "--porcelain"], wt) != ""


def discard_changes(wt: Path) -> None:
    """Throw away uncommitted edits (excluded .task/ files are kept)."""
    git(["reset", "-q", "--hard", "HEAD"], wt, check=False)
    git(["clean", "-fdq"], wt, check=False)


def checkpoint(wt: Path, message: str) -> str | None:
    """Commit everything in the worktree (except excluded .task/). Returns sha or None."""
    if not dirty(wt):
        return None
    git(["add", "-A"], wt)
    git([*ident(wt), "commit", "--no-verify", "-q", "-m", message], wt)
    return git(["rev-parse", "--short", "HEAD"], wt)


def snapshot(wt: Path, ref: str) -> str | None:
    """Record the current working state under `ref` WITHOUT touching index/branch/files.

    Safe to call while an agent is still editing (used for mid-run recovery points).
    """
    sha = git([*ident(wt), "stash", "create"], wt, check=False)
    if not sha:
        return None
    git(["update-ref", ref, sha], wt)
    return sha


def diff_stat(wt: Path, base: str) -> str:
    committed = git(["diff", "--stat", f"{base}...HEAD"], wt, check=False)
    pending = git(["status", "--short"], wt, check=False)
    parts = []
    if committed:
        parts.append(committed)
    if pending:
        parts.append("Uncommitted:\n" + pending)
    return "\n".join(parts) or "(no changes)"


def squash_merge(integ_wt: Path, branch: str, message: str) -> tuple[bool, str]:
    """Squash-merge `branch` into the branch checked out at integ_wt. Returns (ok, detail)."""
    # ident(): a squash that is not a fast-forward (the integration branch moved on, e.g. a
    # parallel task merged first) needs a committer identity even though it does not commit.
    p = subprocess.run(
        ["git", *ident(integ_wt), "merge", "--squash", branch],
        cwd=str(integ_wt),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=child_env(),
    )
    if p.returncode != 0:
        detail = (p.stdout + p.stderr).decode("utf-8", errors="replace")
        abort_merge(integ_wt)
        return False, detail.strip()
    if not dirty(integ_wt):
        return True, "nothing to merge"
    git([*ident(integ_wt), "commit", "--no-verify", "-q", "-m", message], integ_wt)
    return True, git(["rev-parse", "--short", "HEAD"], integ_wt)


def abort_merge(integ_wt: Path) -> None:
    git(["reset", "--hard", "-q", "HEAD"], integ_wt, check=False)
    git(["clean", "-fdq"], integ_wt, check=False)


def undo_last_commit(integ_wt: Path) -> None:
    git(["reset", "--hard", "-q", "HEAD~1"], integ_wt, check=False)
