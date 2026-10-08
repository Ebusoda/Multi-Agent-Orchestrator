"""V2-7: project-level checkpoint for human-driven AI sessions (`orch handoff export`).

Deterministic (no LLM). A new chat session, with any model, reads this one file instead of the
whole history: project memory, what each task is doing, what waits for the user, recent merges,
routing and the cost picture. It is bounded: merged tasks are counted, not listed.

Written to <project>/.agents/PROJECT_HANDOFF.md and committed as docs/project/CHECKPOINT.md on the
integration branch, so it is versioned with the project. Dates only (no clock times), so an
unchanged project produces an unchanged file and no new commit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import memory
from . import workspace as ws
from .models import TaskStatus
from .report import build_report
from .i18n import tr

CHECKPOINT_FILE = "CHECKPOINT.md"
_KEEP = ("goal", "constraint", "architecture", "knowledge", "rejected")


def render(orch) -> str:
    out = [tr("# 项目交接（Checkpoint）：{0}", orch.project.name), "",
           tr("由 `orch handoff export` 确定性生成（不调用模型），{0}。新会话先读这个文件，需要细节再打开下面提到的文件。", time.strftime('%Y-%m-%d')), ""]

    entries = memory.load(orch)
    out.append(tr("## 项目记忆（docs/project/）"))
    if not entries:
        out.append(tr("- 还没有项目记忆：运行 `orch memory init`，再填写目标、约束、架构。"))
    titles = {"goal": tr("目标"), "constraint": tr("约束"), "architecture": tr("架构"), "knowledge": tr("长期知识"),
              "rejected": tr("放弃过的方案")}
    for kind in _KEEP:
        lines = [e.text for e in entries if e.kind == kind]
        if lines:
            out.append(tr("- {0}：", titles[kind]) + tr("；").join(lines))
    n_cand = sum(1 for e in entries if e.kind == "candidate")
    if n_cand:
        out.append(tr("- 待确认的候选 {0} 条：见 docs/project/MEMORY.md", n_cand))
    out.append("")

    tasks = orch.store.list_tasks()
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    out.append(tr("## 任务"))
    out.append("- " + (tr("，").join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else tr("还没有任务")))
    for t in tasks:
        if t.status not in (TaskStatus.MERGED, TaskStatus.CANCELLED):
            note = tr("：{0}", t.note[:160]) if t.note else ""
            out.append(f"- {t.id} [{t.status.value}] {t.title}{note}")
    out.append("")

    waiting = []
    for t in tasks:
        if t.status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            what = tr("被卡住") if t.status == TaskStatus.BLOCKED else tr("失败了")
            waiting.append(tr("- {0} {1}，需要你处理（`orch task show {2}`，之后 `orch task retry {3}`）", t.id, what, t.id, t.id))
        elif t.status == TaskStatus.VERIFIED:
            waiting.append(tr("- {0} 已验收，等合并（`orch merge {1}`）", t.id, t.id))
    for p in orch.store.list_plans():
        if p["status"] == "draft":
            waiting.append(tr("- 计划 {0} 等批准（`orch plan approve {1}`）：{2}", p['id'], p['id'], p['goal'][:80]))
    for d in orch.store.list_decisions():
        if d["status"] == "proposed":
            waiting.append(tr("- 决策 {0} 等拍板（`orch decide accept|reject {1}`）：{2}", d['id'], d['id'], (d['title'] or '')[:80]))
    out.append(tr("## 等你处理"))
    out += waiting or [tr("- 没有")]
    out.append("")

    out.append(tr("## 最近合并（integration 分支）"))
    # the checkpoint's own commits are left out, or every export would change the next one
    log = ws.git(["log", "--format=%h %s", "-10", "--invert-grep", "--grep=^docs: project checkpoint",
                  orch.integration_branch], orch.project, check=False)
    out += [f"- {line}" for line in log.splitlines()] or [tr("- 没有")]
    out.append("")

    out.append(tr("## 路由与池"))
    for key, val in orch.cfg.data.get("routing", {}).items():
        if isinstance(val, dict):
            out += [f"- {key}.{d}: {' -> '.join(v)}" for d, v in val.items()]
        else:
            out.append(f"- {key}: {' -> '.join(val)}")
    for name, pcfg in orch.cfg.pools.items():
        billing = "API" if orch.is_api_pool(name) else tr("订阅")
        out.append(tr("- 池 {0}: {1} {2}（{3}）", name, pcfg.get('executor', '?'), pcfg.get('model') or '', billing).replace("  ", " "))
    out.append("")

    executors = {p: str(c.get("executor", "")) for p, c in orch.cfg.pools.items()}
    rep = build_report(orch.store, orch.cfg.agents_dir, executors)
    out.append(tr("## 执行统计（orch report）"))
    if not rep["groups"]:
        out.append(tr("- 还没有运行记录"))
    for g in rep["groups"]:
        usd = f"${g['usd_per_success']:.4f}" if g["usd_per_success"] is not None else "-"
        out.append(tr("- {0} × {1}：{2} 个任务，成功 {3}，首次通过 {4}，每个成功任务 {5}{6}", g['type'], g['pool'], g['tasks'], g['success'], g['first_pass'], usd, tr('（含估算）') if g['estimated_usd'] else ''))
    out.append("")
    out += [tr("## 怎么继续"),
            tr("- `orch status` 看现状；`orch task show ID` 看单个任务；`orch context ID` 看某次调用注入了什么"),
            tr("- 项目历史在 git 和 .agents/ 里，不要把整段历史贴给模型；按上面的指针打开需要的文件")]
    return "\n".join(out) + "\n"


def export(orch, out: Path | None = None, commit: bool = True) -> tuple[Path, str | None]:
    """Write the checkpoint; commit it to the integration branch when it changed. Returns (path, sha)."""
    text = render(orch)
    path = out or orch.cfg.agents_dir / "PROJECT_HANDOFF.md"
    path.write_text(text, encoding="utf-8")
    sha = None
    # versioned only once the project has opted into versioned memory (`orch memory init`)
    if commit and memory.read_file(orch, "STATE.md") is not None:
        with orch.git_lock:
            wt = ws.ensure_worktree(orch.project, orch.cfg.worktrees_dir / "_integration",
                                    orch.integration_branch, orch.integration_branch)
            ws.abort_merge(wt)
            target = wt / memory.MEMORY_DIR / CHECKPOINT_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            rel = f"{memory.MEMORY_DIR}/{CHECKPOINT_FILE}"
            ws.git(["add", "--", rel], wt)
            if ws.git(["status", "--porcelain", "--", rel], wt):
                ws.git([*ws.ident(wt), "commit", "--no-verify", "-q", "-m", "docs: project checkpoint"], wt)
                sha = ws.git(["rev-parse", "--short", "HEAD"], wt)
    return path, sha


def refresh(orch) -> None:
    """Called after run / merge / plan approve / decide accept. Never fails the command that called it."""
    try:
        _, sha = export(orch)
        if sha:
            orch.store.log("checkpoint", f"docs/project/{CHECKPOINT_FILE} {sha}")
    except (ws.GitError, OSError, json.JSONDecodeError) as e:
        orch.store.log("checkpoint_error", str(e)[:300])
