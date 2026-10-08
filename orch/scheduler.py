"""The deterministic orchestrator loop. No LLM makes control decisions here."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import checkpoint
from . import context as ctxm
from . import handoff as ho
from . import memory
from . import workspace as ws
from .adapters import RunContext
from .config import DEFAULT_CONFIG_TOML, Config
from .models import INFRA_FAILURES, Run, RunResult, RunStatus, Task, TaskStatus
from .proc import STOP, Slots, kill_tree, pid_alive
from .router import Breakers, Router
from .store import Store
from .verify import run_verify


class OrchError(RuntimeError):
    pass


def _norm(path: str) -> str:
    path = path.replace("\\", "/").strip().rstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path or "."


def _has_markers(text: str) -> bool:
    return any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======" for line in text.splitlines())


def review_needed(task: Task, cfg: dict) -> tuple[bool, str]:
    """V2-3: deterministic checks first. An LLM review runs only where they are weakest.

    task.review = always / never wins. Otherwise review when the task's risk is in
    [review] require_risk (default high, critical), or when it has no acceptance command
    (when_no_verify, default true). skip_risk (older setting) still turns review off.
    """
    if not cfg.get("enabled", True):
        return False, "review disabled"
    if task.review == "never":
        return False, "task says review=never"
    if task.review == "always":
        return True, "task says review=always"
    if task.risk in (cfg.get("skip_risk") or []):
        return False, f"risk={task.risk}"
    if task.risk in (cfg.get("require_risk") or ["high", "critical"]):
        return True, f"risk={task.risk}"
    if not task.verify and cfg.get("when_no_verify", True):
        return True, "no acceptance commands"
    return False, f"risk={task.risk} with acceptance commands"


def failure_fingerprint(log: str) -> str:
    """Identify a failing acceptance run, ignoring what changes between identical failures (timings, addresses)."""
    text = re.sub(r"\[exit (\S+) in [\d.]+s\]", r"[exit \1]", log)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|seconds)\b", "#s", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x#", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def scopes_conflict(a: Task, b: Task) -> bool:
    """May these two tasks touch the same files? No scope = could touch anything."""
    if not a.scope or not b.scope:
        return True
    for x in map(_norm, a.scope):
        for y in map(_norm, b.scope):
            if x == "." or y == "." or x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def init_project(project: Path) -> list[str]:
    """Create .agents/, config, DB, git excludes and the integration branch."""
    project = project.resolve()
    if not ws.is_repo(project):
        raise OrchError(f"{project} 不是 git 仓库。先运行: git init && git add -A && git commit -m init")
    if not ws.has_commits(project):
        raise OrchError("仓库还没有任何提交。先提交一次（git add -A && git commit -m init）。")
    msgs = []
    agents = project / ".agents"
    agents.mkdir(exist_ok=True)
    cfg_path = agents / "orch.toml"
    if not cfg_path.exists():
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        msgs.append(f"写入配置 {cfg_path}")
    cfg = Config.load(project)
    Store(agents / "state.db").close()
    ws.setup_excludes(project)
    base = cfg.data["project"].get("base_branch") or ws.current_branch(project)
    integ = cfg.data["project"]["integration_branch"]
    ws.ensure_branch(project, integ, base)
    msgs.append(f"integration 分支: {integ}（起点 {base}）")
    msgs.append(f"worktree 目录: {cfg.worktrees_dir}")
    return msgs


class Orchestrator:
    def __init__(self, project: Path, say: Callable[[str], None] = print,
                 slots: Slots | None = None, git_lock: threading.RLock | None = None):
        self.project = project.resolve()
        if not (self.project / ".agents").is_dir():
            raise OrchError(f"{self.project} 还没有初始化。先运行: orch init")
        self.cfg = Config.load(self.project)
        self.store = Store(self.cfg.agents_dir / "state.db")  # one SQLite connection per thread
        self.breakers = Breakers(self.store, self.cfg)
        self.router = Router(self.cfg, self.store, self.breakers)
        self.say = say
        # shared between the main thread and its workers
        self.slots = slots or Slots({p: c.get("max_concurrent", 1) for p, c in self.cfg.pools.items()})
        self.git_lock = git_lock or threading.RLock()  # worktree add/remove/prune and merges

    # -- helpers -------------------------------------------------------------
    @property
    def integration_branch(self) -> str:
        return self.cfg.data["project"]["integration_branch"]

    def task_dir(self, task_id: str) -> Path:
        d = self.cfg.agents_dir / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _minutes(self, key: str, default: float) -> float:
        return float(self.cfg.esc.get(key, default)) * 60

    def acquire(self, task: Task, pool: str) -> bool:
        """Take a run slot on `pool` (waits if it is busy). False = the pool's breaker opened
        while waiting; pick again. On Ctrl+C while waiting the task goes back to the queue."""
        def on_wait() -> None:
            self.say(f"[{task.id}] 等待 {pool} 空出来（同时最多 {self.slots.limit(pool)} 个）")

        try:
            waited = self.slots.acquire(pool, on_wait)
        except KeyboardInterrupt:
            if task.status == TaskStatus.RUNNING:
                self._finish(task, TaskStatus.QUEUED, "stopped by user while waiting for a pool")
            raise
        if waited and self.breakers.is_open(pool):
            self.slots.release(pool)
            return False
        return True

    @contextmanager
    def lock(self) -> Iterator[None]:
        path = self.cfg.agents_dir / "orch.lock"
        if path.exists():
            try:
                other = int(path.read_text().strip() or 0)
            except ValueError:
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                raise OrchError(f"另一个 orch 进程（pid {other}）正在运行这个项目。")
        path.write_text(str(os.getpid()))
        try:
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    # -- recovery ------------------------------------------------------------
    def recover(self) -> None:
        """Runs left 'running' by a dead orchestrator become INTERRUPTED -> handoff path."""
        for run in self.store.running_runs():
            if pid_alive(run.pid):
                self.say(f"! {run.id} 的进程 {run.pid} 还活着但已无人管理，正在停止它")
                kill_tree(int(run.pid))  # type: ignore[arg-type]
            run.status = RunStatus.INTERRUPTED
            run.ended_at = run.ended_at or time.time()
            run.error = run.error or "orchestrator stopped while this run was in progress"
            self.store.save_run(run)
            self.store.log("recovered", f"{run.id} marked interrupted", task_id=run.task_id, run_id=run.id)
        for task in self.store.list_tasks([TaskStatus.RUNNING]):
            task.status = TaskStatus.QUEUED
            task.note = "interrupted; next run takes over with a handoff"
            self.store.save_task(task)

    # -- main loop -----------------------------------------------------------
    def runnable(self, only: set[str] | None = None) -> list[Task]:
        merged = {t.id for t in self.store.list_tasks([TaskStatus.MERGED])}
        out = []
        for t in self.store.list_tasks([TaskStatus.QUEUED]):
            if only and t.id not in only:
                continue
            if all(d in merged for d in t.depends_on):
                out.append(t)
        return out

    def run(self, only: list[str] | None = None, max_tasks: int | None = None, auto_merge: bool = False,
            jobs: int | None = None) -> None:
        if jobs is None:
            jobs = int(self.cfg.data.get("parallel", {}).get("max_jobs", 1) or 1)
        if jobs > 1:
            return self._run_parallel(only, max_tasks, auto_merge, jobs)
        with self.lock():
            self.recover()
            STOP.clear()
            done = 0
            attempted: set[str] = set()
            while True:
                tasks = [t for t in self.runnable(set(only) if only else None) if t.id not in attempted]
                if not tasks:
                    break
                task = tasks[0]
                attempted.add(task.id)
                self.run_task(task)
                done += 1
                if auto_merge and task.status == TaskStatus.VERIFIED and self._may_auto_merge(task):
                    self.merge(task.id)
                if max_tasks and done >= max_tasks:
                    break
            waiting = [t for t in self.store.list_tasks([TaskStatus.QUEUED]) if t.id not in attempted]
            if waiting:
                ids = ", ".join(t.id for t in waiting)
                self.say(f"仍在排队（依赖未合并）: {ids}")
            if done == 0 and not waiting:
                self.say("没有可运行的任务。")
            else:
                checkpoint.refresh(self)

    # -- parallel --------------------------------------------------------------
    def _run_parallel(self, only: list[str] | None, max_tasks: int | None, auto_merge: bool, jobs: int) -> None:
        """Up to `jobs` tasks at once, each in its own thread, worktree and DB connection.

        The main thread only schedules and merges (one merge at a time). A task is started when
        it is runnable, its scope does not overlap a running task, and the pool it would start on
        has a free slot (so a busy cheap pool is waited for, never skipped for a pricier one).
        """
        with self.lock():
            self.recover()
            STOP.clear()
            only_set = set(only) if only else None
            done_q: queue.Queue[str] = queue.Queue()
            running: dict[str, tuple[threading.Thread, Task]] = {}
            attempted: set[str] = set()
            launched = 0
            seen_version = -1
            changed = True
            self.say(f"并行模式：最多同时 {jobs} 个任务")
            try:
                while True:
                    while True:  # collect finished workers
                        try:
                            tid = done_q.get_nowait()
                        except queue.Empty:
                            break
                        thread, _t = running.pop(tid)
                        thread.join()
                        changed = True
                        task = self.store.get_task(tid)
                        if (auto_merge and task and task.status == TaskStatus.VERIFIED
                                and self._may_auto_merge(task)):
                            self.merge(tid)
                    if changed or self.slots.version != seen_version:
                        changed = False
                        seen_version = self.slots.version
                        for t in self.runnable(only_set):
                            if len(running) >= jobs or (max_tasks and launched >= max_tasks):
                                break
                            if t.id in attempted:
                                continue
                            if any(scopes_conflict(t, other) for _th, other in running.values()):
                                continue
                            picked = self.router.pick(t)
                            if picked is not None and not self.slots.free(picked[1]):
                                continue
                            attempted.add(t.id)
                            launched += 1
                            th = threading.Thread(target=self._worker, args=(t.id, done_q),
                                                  name=f"orch-{t.id}", daemon=True)
                            running[t.id] = (th, t)
                            th.start()
                    if not running:
                        break
                    try:
                        done_q.put(done_q.get(timeout=1.0))  # wake on a finished worker, else poll
                    except queue.Empty:
                        pass
            except KeyboardInterrupt:
                STOP.set()
                self.say("正在停止所有运行中的任务（每个都会先做 checkpoint）...")
                for th, _t in running.values():
                    th.join()
                raise
            waiting = [t for t in self.store.list_tasks([TaskStatus.QUEUED]) if t.id not in attempted]
            if waiting:
                self.say("仍在排队（依赖未合并）: " + ", ".join(t.id for t in waiting))
            if launched == 0 and not waiting:
                self.say("没有可运行的任务。")
            else:
                checkpoint.refresh(self)

    def _worker(self, task_id: str, done_q: "queue.Queue[str]") -> None:
        o = Orchestrator(self.project, self.say, slots=self.slots, git_lock=self.git_lock)
        try:
            task = o.store.get_task(task_id)
            if task is not None:
                o.run_task(task)
        except KeyboardInterrupt:
            pass  # the task already went back to the queue with a note
        except Exception as e:  # never let one task take the whole run down
            o.store.log("orch_error", traceback.format_exc()[-2000:], task_id=task_id)
            task = o.store.get_task(task_id)
            if task is not None:
                o._finish(task, TaskStatus.BLOCKED, f"orchestrator error: {e!r}"[:500])
        finally:
            o.store.close()
            done_q.put(task_id)

    def run_task(self, task: Task) -> Task:
        route = self.router.route(task)
        if not route:
            return self._finish(task, TaskStatus.BLOCKED, f"no routing rule for type={task.type}")
        integ = self.integration_branch
        with self.git_lock:
            wt = ws.ensure_worktree(self.project, self.cfg.worktrees_dir / task.id, f"task/{task.id}", integ)
        task.branch, task.worktree = f"task/{task.id}", str(wt)
        task.status = TaskStatus.RUNNING
        self.store.save_task(task)
        # Work left uncommitted by a run that never reached its checkpoint (orchestrator
        # killed, power loss): commit it now so the next agent and the history both see it.
        sha = ws.checkpoint(wt, f"wip({task.id}): recovered uncommitted work")
        if sha:
            self.say(f"[{task.id}] 发现上次未提交的修改，已补做 checkpoint {sha}")

        all_runs = self.store.runs_for(task.id)
        runs = [r for r in all_runs if r.kind == "work"]
        last: Run | None = runs[-1] if runs else None
        total_runs = len(runs)
        # The newest run is a review that rejected the work: the next run addresses it. Read back
        # from the database, so this also holds after a restart or `orch task retry`.
        review_fix = bool(last and all_runs[-1].kind == "review" and all_runs[-1].verified is False)
        last_verify_log = ""
        threshold = int(self.cfg.esc.get("verify_failures", 2))
        early_on = bool(self.cfg.esc.get("early_escalation", True))
        prev_failure: tuple[str, str] | None = None   # (pool, fingerprint) of the previous failed acceptance
        # task.runs_count only counts runs where an agent actually worked. Infrastructure
        # failures (crash / rate limit / auth) are bounded by the breakers instead, plus
        # this safety cap so a misconfiguration can never loop forever.
        infra_cap = task.max_runs + len(route) * int(self.cfg.esc.get("breaker_threshold", 2))

        if last and last.verified is True and all_runs[-1].kind == "review" and all_runs[-1].verified is None:
            # The work already passed acceptance; only its review was cut short.
            self.say(f"[{task.id}] 上次的审查没有完成，重新审查（不重做任务）")
            done = self._after_pass(task, wt, last.pool, self._vendor_of(last.pool))
            if done is not None:
                return done
            review_fix = True

        while True:
            over = self._over_budget(task)
            if over:
                return self._finish(task, TaskStatus.BLOCKED, over)
            if task.runs_count >= task.max_runs:
                return self._finish(task, TaskStatus.FAILED, f"reached max_runs={task.max_runs}")
            if total_runs >= infra_cap:
                return self._finish(task, TaskStatus.BLOCKED, f"too many runs ({total_runs}); check executors")
            picked = self.router.pick(task)
            if picked is None:
                return self._finish(
                    task, TaskStatus.BLOCKED,
                    f"no available pool at escalation level {task.ladder} of {route}",
                )
            idx, pool, adapter = picked

            # Decide how to start: fresh / fix own failed attempt (resume) / take over.
            if last is not None and last.pool != pool:
                task.verify_failures = 0
            rot = self._rotation_reason(task, last) if last is not None and last.pool == pool else None
            if last is None:
                mode, resume = "fresh", None
            elif review_fix:
                if (last.pool == pool and last.session_id and last.status not in INFRA_FAILURES
                        and adapter.supports_resume() and not rot):
                    mode, resume = "address_review", last.session_id
                else:
                    mode, resume = "review_takeover", None
                    reason = rot or (f"A reviewer asked for changes to the work of '{last.pool}', "
                                     f"which is not available now.")
                    ho.write_handoff(wt, ho.build_handoff(task, last, wt, integ, last_verify_log, reason))
            elif (
                last.pool == pool
                and last.verified is False
                and last.session_id
                and last.status in (RunStatus.COMPLETED, RunStatus.TASK_FAILED, RunStatus.TIMEOUT)
                and adapter.supports_resume()
                and not rot
            ):
                mode, resume = "fix_verify", last.session_id
            else:
                mode, resume = "takeover", None
                reason = rot or self._handoff_reason(last, pool)
                ho.write_handoff(wt, ho.build_handoff(task, last, wt, integ, last_verify_log, reason))

            if rot and resume is None:
                self.say(f"[{task.id}] 会话轮换：{rot}")
                self.store.log("session_rotated", rot, task_id=task.id, run_id=last.id if last else None)
            ho.write_task_files(wt, task, mode)
            manifest = ctxm.build(self, task, wt, mode)
            manifest["inlined"] = ho.inline_files(wt / ".task" / "PROMPT.md",
                                                  ctxm.MODE_FILES.get(mode, ["TASK.md"]) + ["CONTEXT.md"])
            if rot and resume is None:
                manifest["rotation"] = rot
            if not self.acquire(task, pool):
                continue  # its breaker opened while we waited: pick again
            try:
                run = self.store.create_run(task.id, pool, adapter.executor, resumed=bool(resume))
                events = self.task_dir(task.id) / "runs" / f"{run.id}.jsonl"
                run.events_path = str(events)
                run.context_tokens = manifest["total"]
                run.context_manifest = ctxm.dumps(manifest)
                self.store.save_run(run)

                def on_start(pid: int, run: Run = run) -> None:
                    run.pid = pid
                    self.store.save_run(run)

                self.say(f"[{task.id}] {run.id} -> {pool} ({adapter.executor}), mode={mode}")
                self.say(f"[{task.id}] {run.id} {ctxm.summary_line(manifest)}")
                ctx = RunContext(
                    task=task, worktree=wt, events_path=events,
                    timeout_s=self._minutes("run_timeout_minutes", 45),
                    model=adapter.model, resume_session=resume, on_start=on_start,
                )
                result = adapter.run(ctx)
            finally:
                self.slots.release(pool)

            run.status = result.status
            run.session_id = result.session_id
            run.exit_code = result.exit_code
            run.est_cost_usd = result.est_cost_usd
            run.tokens_in, run.tokens_out = result.tokens_in, result.tokens_out
            run.tokens_cached, run.model_calls = result.tokens_cached, result.model_calls
            run.resets_at = result.resets_at
            run.quota = json.dumps(result.quota) if result.quota else ""
            run.summary = (result.summary or "")[-4000:]
            run.error = (result.error or "")[-4000:]
            run.ended_at = time.time()
            total_runs += 1
            if result.status not in INFRA_FAILURES:
                task.runs_count += 1
            sha = ws.checkpoint(wt, f"wip({task.id}): checkpoint [{pool}] {run.id} {result.status.value}")
            ho.archive(wt, self.task_dir(task.id), run.id)
            self.store.save_run(run)
            cp = f", checkpoint {sha}" if sha else ""
            self.say(f"[{task.id}] {run.id} ended: {result.status.value}{cp}")
            self.store.log("run_end", f"{result.status.value}{cp}", task_id=task.id, run_id=run.id)
            last = run
            if result.status not in INFRA_FAILURES:
                review_fix = False

            if result.status in INFRA_FAILURES:
                if result.status != RunStatus.INTERRUPTED:  # Ctrl+C is not the executor's fault
                    self.breakers.record_failure(pool, result.status, result.resets_at)
                self.store.save_task(task)
                if result.status == RunStatus.INTERRUPTED:
                    self._finish(task, TaskStatus.QUEUED, "interrupted by user; next run takes over")
                    raise KeyboardInterrupt
                continue  # router skips the open breaker; the next run gets a handoff

            self.breakers.record_success(pool)
            blocked = ho.blocked_reason(wt)
            passed, log = run_verify(
                task.verify, wt, self.task_dir(task.id) / "verify.log",
                self._minutes("verify_timeout_minutes", 15),
            )
            if STOP.is_set():  # Ctrl+C during acceptance (parallel mode): not the agent's failure
                self._finish(task, TaskStatus.QUEUED, "stopped by user during acceptance; next run checks again")
                raise KeyboardInterrupt
            ho.write_verify_log(wt, log)
            last_verify_log = log
            run.verified = passed
            self.store.save_run(run)

            if passed and not blocked:
                task.verify_failures = 0
                self.say(f"[{task.id}] 验收通过")
                done = self._after_pass(task, wt, pool, adapter.vendor)
                if done is not None:
                    return done
                review_fix = True  # the reviewer sent it back to the author
                continue

            task.verify_failures += 1
            self.say(f"[{task.id}] 验收未通过（{pool} 连续第 {task.verify_failures} 次）"
                     + (f"；agent 报告 BLOCKED: {blocked}" if blocked else ""))
            # Early escalation: do not spend the remaining attempts on a pool that is clearly stuck.
            fp = failure_fingerprint(log)
            early = None
            if early_on and not blocked and task.verify_failures < threshold:
                if sha is None:
                    early = ("no files changed", "这次运行没有改动任何文件")
                elif prev_failure == (pool, fp):
                    early = ("same failure as the previous run", "验收报错和上一次完全一样")
            prev_failure = (pool, fp)
            if early:
                self.say(f"[{task.id}] 提前升级：{early[1]}")
                self.store.log("early_escalate", f"{pool}: {early[0]}", task_id=task.id, run_id=run.id)
            if blocked or early or task.verify_failures >= threshold:
                task.ladder = idx + 1
                task.verify_failures = 0
                nxt = route[task.ladder] if task.ladder < len(route) else "（无）"
                self.say(f"[{task.id}] 升级: {pool} -> {nxt}")
                self.store.log("escalate", f"{pool} -> {nxt}", task_id=task.id, run_id=run.id)
            self.store.save_task(task)

    def _vendor_of(self, pool: str) -> str:
        from .adapters import make_adapter

        try:
            return make_adapter(self.cfg, pool).vendor
        except KeyError:
            return pool

    def _after_pass(self, task: Task, wt: Path, pool: str, vendor: str) -> Task | None:
        """Acceptance passed: review it. Returns the finished task, or None = back to the author."""
        try:
            outcome, rnote = self._review(task, wt, pool, vendor)
        except KeyboardInterrupt:
            self._finish(task, TaskStatus.QUEUED, "review interrupted by user; next run reviews again")
            raise
        if outcome == "changes":
            task.review_rounds += 1
            limit = int(self.cfg.data.get("review", {}).get("max_fix_rounds", 1))
            if task.review_rounds > limit:
                return self._finish(
                    task, TaskStatus.BLOCKED,
                    f"{rnote}; still not approved after {limit} fix round(s). Read "
                    f"{self.task_dir(task.id) / 'REVIEW.md'}, then `orch task accept {task.id}` to merge "
                    f"anyway or `orch task retry {task.id}` for one more round",
                )
            self.say(f"[{task.id}] 审查退回（第 {task.review_rounds} 次），交给作者按 REVIEW.md 修改")
            self.store.save_task(task)
            return None
        notes = [] if task.verify else ["WARNING: no verify commands, accepted on the agent's word"]
        notes.append(rnote)
        return self._finish(task, TaskStatus.VERIFIED, "; ".join(n for n in notes if n))

    def _review(self, task: Task, wt: Path, author_pool: str, author_vendor: str) -> tuple[str, str]:
        from .review import run_review

        need, why = review_needed(task, self.cfg.data.get("review", {}))
        if not need:
            return "skipped", f"review skipped ({why})"
        outcome, note = run_review(self, task, wt, author_pool, author_vendor)
        labels = {"approved": "审查通过", "changes": "审查要求修改", "skipped": "审查跳过"}
        self.say(f"[{task.id}] {labels.get(outcome, outcome)}: {note}")
        self.store.log("review_" + outcome, note[:200], task_id=task.id)
        return outcome, note

    def is_api_pool(self, pool: str) -> bool:
        """Pay-per-use pools spend real money; subscription pools spend quota. [pools.X] billing overrides."""
        pcfg = self.cfg.pools.get(pool, {})
        billing = pcfg.get("billing") or ("api" if pcfg.get("executor") in ("opencode", "api") else "subscription")
        return billing == "api"

    def task_spend(self, task_id: str) -> tuple[float, int]:
        """(dollars spent on API pools, tokens in + out on all pools) by a task's work and review runs."""
        usd, tokens = 0.0, 0
        for r in self.store.runs_for(task_id):
            if r.est_cost_usd and self.is_api_pool(r.pool):
                usd += r.est_cost_usd
            tokens += (r.tokens_in or 0) + (r.tokens_out or 0)
        return usd, tokens

    def _over_budget(self, task: Task) -> str | None:
        """V2-6: stop a task and hand it to the user once it has cost more than [budget] allows."""
        b = self.cfg.data.get("budget", {})
        max_usd = float(b.get("max_usd_per_task", 0) or 0)
        max_tokens = int(b.get("max_tokens_per_task", 0) or 0)
        usd, tokens = self.task_spend(task.id)
        if max_usd and usd >= max_usd:
            return (f"budget: API spend ${usd:.3f} reached max_usd_per_task ${max_usd:.2f}; raise "
                    f"[budget] max_usd_per_task in orch.toml, then orch task retry {task.id}")
        if max_tokens and tokens >= max_tokens:
            return (f"budget: {tokens} tokens reached max_tokens_per_task {max_tokens}; raise "
                    f"[budget] max_tokens_per_task in orch.toml, then orch task retry {task.id}")
        return None

    def _resolve_conflict(self, task: Task) -> bool:
        """Merge the integration branch into the task's branch and let an agent resolve the conflicts.

        Success needs: no conflict markers left in the conflicted files, and the task's acceptance
        commands passing on the result. Then orch commits the merge on the task branch, and the squash
        merge into integration is tried again. Otherwise everything is rolled back. [merge]
        resolve_conflicts = false turns this off."""
        if not self.cfg.data.get("merge", {}).get("resolve_conflicts", True):
            return False
        wt = Path(task.worktree)
        if not wt.is_dir():
            return False
        integ = self.integration_branch
        with self.git_lock:
            ws.git([*ws.ident(wt), "merge", "--no-ff", "--no-commit", integ], wt, check=False)
        files = [f for f in ws.git(["diff", "--name-only", "--diff-filter=U"], wt, check=False).splitlines() if f]
        if not files:  # git merged it cleanly in this direction
            ws.git([*ws.ident(wt), "commit", "--no-verify", "-q", "-m", f"merge {integ} into {task.branch}"],
                   wt, check=False)
            return True
        self.say(f"[{task.id}] 合并冲突：{', '.join(files)}，交给 agent 解决")
        d = wt / ".task"
        d.mkdir(exist_ok=True)
        (d / "CONFLICTS.md").write_text("# Files with merge conflicts\n\n" + "\n".join(f"- `{f}`" for f in files) + "\n",
                                        encoding="utf-8")
        route = self.router.route(task)
        last = [r for r in self.store.runs_for(task.id, kind="work")]
        order = ([last[-1].pool] if last and last[-1].pool in route else []) + route
        resolved = False
        for pool in dict.fromkeys(order):
            picked = self.router.pick_from(task, [pool])
            if picked is None:
                continue
            _, pool, adapter = picked
            result = self._agent_call(task, wt, pool, adapter, "resolve_conflict")
            if result.status not in INFRA_FAILURES:
                break
        else:
            result = None
        if result is not None and result.status == RunStatus.COMPLETED:
            left = [f for f in files if (wt / f).is_file() and _has_markers((wt / f).read_text(encoding="utf-8", errors="replace"))]
            passed, _ = run_verify(task.verify, wt, self.task_dir(task.id) / "conflict_verify.log",
                                   self._minutes("verify_timeout_minutes", 15))
            resolved = not left and passed
            if left:
                self.say(f"[{task.id}] 冲突标记还在：{', '.join(left)}")
        if resolved:
            with self.git_lock:
                ws.git(["add", "-A"], wt)
                ws.git([*ws.ident(wt), "commit", "--no-verify", "-q", "-m",
                        f"merge {integ} into {task.branch} (conflicts resolved by {pool})"], wt)
            self.store.log("conflict_resolved", f"{pool}: {', '.join(files)}", task_id=task.id)
            self.say(f"[{task.id}] 冲突已由 {pool} 解决，验收通过")
            return True
        with self.git_lock:
            ws.git(["merge", "--abort"], wt, check=False)
            ws.git(["reset", "--hard", "-q", "HEAD"], wt, check=False)
        self.store.log("conflict_unresolved", ", ".join(files), task_id=task.id)
        return False

    def _agent_call(self, task: Task, wt: Path, pool: str, adapter, mode: str):
        """One agent run outside the main loop (conflict resolution): prompt, context, run record."""
        ho.write_task_files(wt, task, mode)
        manifest = ctxm.build(self, task, wt, mode)
        manifest["inlined"] = ho.inline_files(wt / ".task" / "PROMPT.md", ctxm.MODE_FILES.get(mode, ["TASK.md"]) + ["CONTEXT.md"])
        if not self.acquire(task, pool):
            return RunResult(status=RunStatus.UNAVAILABLE, error="pool busy and its breaker opened")
        try:
            run = self.store.create_run(task.id, pool, adapter.executor, resumed=False)
            events = self.task_dir(task.id) / "runs" / f"{run.id}.jsonl"
            run.events_path = str(events)
            run.context_tokens, run.context_manifest = manifest["total"], ctxm.dumps(manifest)
            self.store.save_run(run)
            self.say(f"[{task.id}] {run.id} -> {pool} ({adapter.executor}), mode={mode}")
            result = adapter.run(RunContext(task=task, worktree=wt, events_path=events,
                                            timeout_s=self._minutes("run_timeout_minutes", 45), model=adapter.model))
        finally:
            self.slots.release(pool)
        from .plan import _finish_run

        _finish_run(self.store, run, result)
        if result.status in INFRA_FAILURES:
            self.breakers.record_failure(pool, result.status, result.resets_at)
        else:
            self.breakers.record_success(pool)
        ho.archive(wt, self.task_dir(task.id), run.id)
        return result

    def _may_auto_merge(self, task: Task) -> bool:
        """High-risk work waits for a person even with --auto-merge (V2-3)."""
        manual = self.cfg.data.get("approval", {}).get("manual_merge_risk", ["high", "critical"]) or []
        if task.risk not in manual:
            return True
        self.say(f"[{task.id}] risk={task.risk}：需要你确认后再合并（orch merge {task.id}）")
        self.store.log("awaiting_approval", f"risk={task.risk}; run orch merge {task.id}", task_id=task.id)
        return False

    def _rotation_reason(self, task: Task, last: Run) -> str | None:
        """V2-7: a resumed session grows with every turn, and so does each call's cost. Start a fresh
        session (with HANDOFF.md + CONTEXT.md) once the last call was big or the session was resumed
        often enough. [context] resume_max_tokens / max_resumes_per_session; a pool may set its own."""
        if not last.session_id:
            return None
        ccfg = self.cfg.data.get("context", {})
        pcfg = self.cfg.pools.get(last.pool, {})
        limit = int(pcfg.get("resume_max_tokens", ccfg.get("resume_max_tokens", 100_000)) or 0)
        max_resumes = int(pcfg.get("max_resumes_per_session", ccfg.get("max_resumes_per_session", 2)) or 0)
        if limit and (last.tokens_in or 0) >= limit:
            return (f"the previous call in this session read {last.tokens_in} input tokens "
                    f"(limit {limit}); continuing in a fresh session from the handoff note")
        same = [r for r in self.store.runs_for(task.id, kind="work") if r.session_id == last.session_id]
        if max_resumes and len(same) - 1 >= max_resumes:
            return (f"this session was already resumed {len(same) - 1} time(s) (limit {max_resumes}); "
                    f"continuing in a fresh session from the handoff note")
        return None

    def _handoff_reason(self, last: Run, next_pool: str) -> str:
        if last.status in INFRA_FAILURES:
            return f"The previous run on '{last.pool}' ended with '{last.status.value}' (infrastructure problem, not your fault)."
        if last.pool != next_pool:
            return f"The task was escalated from '{last.pool}' to '{next_pool}' after acceptance kept failing."
        return f"The previous session on '{last.pool}' could not be resumed; continuing in a fresh session."

    def _finish(self, task: Task, status: TaskStatus, note: str) -> Task:
        task.status = status
        task.note = note
        self.store.save_task(task)
        self.store.log("task_" + status.value, note, task_id=task.id)
        if status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            self.say(f"[{task.id}] {status.value}: {note}")
        return task

    # -- merge ---------------------------------------------------------------
    def merge(self, task_id: str, keep_worktree: bool = False) -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            raise OrchError(f"没有任务 {task_id}")
        if task.status != TaskStatus.VERIFIED:
            raise OrchError(f"{task_id} 状态是 {task.status.value}，只有 verified 的任务可以合并")
        integ = self.integration_branch
        with self.git_lock:
            integ_wt = ws.ensure_worktree(self.project, self.cfg.worktrees_dir / "_integration", integ, integ)
            ws.abort_merge(integ_wt)
            ok, detail = ws.squash_merge(integ_wt, task.branch, f"{task.id}: {task.title}")
        conflict = not ok and ("CONFLICT" in detail or "conflict" in detail.lower())
        if conflict and self._resolve_conflict(task):
            with self.git_lock:
                ws.abort_merge(integ_wt)
                ok, detail = ws.squash_merge(integ_wt, task.branch, f"{task.id}: {task.title}")
            conflict = not ok and ("CONFLICT" in detail or "conflict" in detail.lower())
        if not ok:
            what = "merge conflict with integration branch" if conflict else "merge failed"
            task.note = f"{what}: " + detail[-500:]
            self.store.save_task(task)
            self.say(f"[{task_id}] {'合并冲突' if conflict else '合并失败'}，已回滚。详情: {detail[-300:]}")
            return False
        # the task's own acceptance, then the project-wide baseline (V2-5): another task's tests
        # must still pass after this one lands, or the merge is undone
        baseline = [c for c in self.cfg.data.get("merge", {}).get("baseline", []) or [] if c not in task.verify]
        passed, _ = run_verify(
            task.verify + baseline, integ_wt, self.task_dir(task_id) / "merge_verify.log",
            self._minutes("verify_timeout_minutes", 15),
        )
        if not passed:
            if detail != "nothing to merge":
                with self.git_lock:
                    ws.undo_last_commit(integ_wt)
            what = "acceptance or baseline" if baseline else "acceptance"
            task.note = (f"merged result failed {what} on the integration branch; merge undone "
                         f"(see {self.task_dir(task_id) / 'merge_verify.log'})")
            self.store.save_task(task)
            self.say(f"[{task_id}] 合并后在 integration 分支验收失败，已撤销合并")
            return False
        task.status = TaskStatus.MERGED
        task.note = f"merged as {detail}"
        self.store.save_task(task)
        self.store.log("merged", detail, task_id=task_id)
        try:
            with self.git_lock:
                msha = memory.on_merge(self, task, detail)
            if msha:
                self.say(f"[{task_id}] 项目记忆已更新（docs/project/，{msha}）")
        except ws.GitError as e:  # memory is a convenience; never undo a good merge for it
            self.store.log("memory_error", str(e)[:300], task_id=task_id)
        if not keep_worktree:
            with self.git_lock:
                ws.remove_worktree(self.project, Path(task.worktree))
        self.say(f"[{task_id}] 已合并到 {integ}（{detail}）")
        return True
