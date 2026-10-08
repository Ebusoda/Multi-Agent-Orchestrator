"""Command line: python -m orch [-p PROJECT] <command> ...

Commands
  init            初始化项目（.agents/、配置、integration 分支）
  doctor          检查 Python / git / 各 CLI 是否可用
  task add|list|show|retry|accept|cancel
  plan "目标"     M4：Manager 只读分析后拆成任务；plan approve P-0001 确认后才会建任务
  decide "问题"   M5：两家各出方案、互相点评、裁判写决策记录（ADR）；decide accept|reject D-0001
  run             运行排队中的任务
  status          任务、熔断器、今日用量
  report          执行记录报表：类型 × 池的首次通过率、每个成功任务的成本
  context ID      某次运行实际注入了哪些上下文：各部分 token、预算、检索、压缩（ID = T-/R-xxxx 或 latest）
  memory init|show|commit   项目记忆（docs/project/，提交在 integration 分支）
  handoff export  项目交接文件（Checkpoint）：给新的 AI 会话读，不调用模型
  key set|list PROVIDER   save your own API keys
  connect [PROVIDER]      which models you can use; add one to the project
  mcp --project DIR   MCP 服务：在聊天应用里查看和派发任务
  route suggest|apply   v0.3：按执行记录给路由建议，你同意后再写进配置
  api check POOL  v0.3：检查直接 API 池（环境变量里的 key、模型列表、一次极小的调用）
  merge T-xxxx    把验收通过的任务合并进 integration 分支
  breaker reset   手动关闭熔断器
  breaker open    手动暂停一个池
  handoff-test    创建真实交接测试项目（Claude 被打断 -> Codex 接班）
  demo [DIR]      创建离线演示项目（假执行者，不花额度）
  probe EXECUTOR  M0：用真实 CLI 跑一个极小任务，记录事件流
"""
from __future__ import annotations

import argparse
import platform
import sys
import time
import tomllib
from pathlib import Path

from .config import Config
from .models import Task, TaskStatus
from .proc import resolve_command, run_capture
from .scheduler import OrchError, Orchestrator, init_project


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve()


_OPENED: list[Orchestrator] = []


def _open(args: argparse.Namespace) -> Orchestrator:
    orch = Orchestrator(_project(args))
    _OPENED.append(orch)  # closed by main()
    return orch


# -- commands -----------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    for m in init_project(_project(args)):
        print(m)
    print("完成。下一步: 编辑 .agents/orch.toml，然后 orch doctor")
    return 0


INSTALL_HINTS = {
    # Windows（PowerShell）; 装完后要开一个新终端，PATH 才会生效
    "git": "winget install Git.Git",
    "claude": "irm https://claude.ai/install.ps1 | iex   或   winget install Anthropic.ClaudeCode",
    "codex": "npm install -g @openai/codex   （需要 Node.js: winget install OpenJS.NodeJS.LTS）",
    "opencode": "npm install -g opencode-ai   （需要 Node.js）",
}


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"OS      : {platform.system()} {platform.release()} ({platform.machine()})")
    py = sys.version_info
    py_ok = py >= (3, 11)
    ok &= py_ok
    print(f"Python  : {platform.python_version()} {'OK' if py_ok else '需要 3.11 以上'}")
    code, out = run_capture(["git", "--version"])
    ok &= code == 0
    print(f"git     : {out if code == 0 else '未找到  -> ' + INSTALL_HINTS['git']}")
    project = _project(args)
    cfg = Config.load(project)
    seen: set[str] = set()
    print("\n执行壳（按 orch.toml 的 [pools]）:")
    for pool, pcfg in cfg.pools.items():
        ex = pcfg.get("executor", "")
        if ex == "fake":
            print(f"  {pool:<12} fake    script={pcfg.get('script')}")
            continue
        if ex == "api":
            from .adapters import make_adapter

            ok_key = make_adapter(cfg, pool).available()
            print(f"  {pool:<12} api          {pcfg.get('model', '')}  key: "
                  + ("set" if ok_key else "missing -> orch key set, then orch api check " + pool))
            continue
        cmd = cfg.executor_cfg(ex).get("command", ex)
        path = resolve_command(cmd)
        line = f"  {pool:<12} {ex:<12} {cmd} -> {path or '未找到'}"
        if not path and cmd in INSTALL_HINTS:
            line += f"\n               安装: {INSTALL_HINTS[cmd]}"
        if path and cmd not in seen:
            seen.add(cmd)
            vcode, vout = run_capture([cmd, "--version"], timeout=30)
            line += f"   [{(vout.splitlines() or ['?'])[0][:60]}]"
        print(line)
    if (project / ".agents").is_dir():
        print(f"\n项目    : {project}（已初始化）  worktrees -> {cfg.worktrees_dir}")
    else:
        print(f"\n项目    : {project}（未初始化，运行 orch init）")
    return 0 if ok else 1


def _task_from_args(args: argparse.Namespace) -> Task:
    if args.file:
        with open(args.file, "rb") as f:
            data = tomllib.load(f)
        data.pop("id", None)
        data.pop("status", None)
        return Task(id="", **data)
    if not args.title:
        raise SystemExit("需要 --title（或用 -f task.toml）")
    spec = args.spec or ""
    if args.spec_file:
        spec = Path(args.spec_file).read_text(encoding="utf-8")
    return Task(
        id="", title=args.title, spec=spec, type=args.type, difficulty=args.difficulty, risk=args.risk,
        scope=args.scope or [], verify=args.verify or [], depends_on=args.depends or [],
        max_runs=args.max_runs or 4, review=args.review,
    )


def cmd_task(args: argparse.Namespace) -> int:
    orch = _open(args)
    if args.task_cmd == "add":
        task = _task_from_args(args)
        if args.max_runs is None:
            task.max_runs = int(orch.cfg.esc.get("max_runs_per_task", task.max_runs))
        same = [t for t in orch.store.list_tasks()
                if t.title.strip().lower() == task.title.strip().lower() and t.status != TaskStatus.CANCELLED]
        if same and not args.allow_duplicate:
            print(f"已经有同名任务 {same[-1].id}（{same[-1].status.value}），没有重复添加。"
                  "确实要再加一个，请带上 --allow-duplicate")
            return 1
        task = orch.store.add_task(task)
        route = orch.router.route(task)
        print(f"已添加 {task.id}: {task.title}  路由: {' -> '.join(route) or '（无匹配规则！）'}")
        if not task.verify:
            print("提醒: 没有 --verify 验收命令，完成与否只能听 agent 自己说。")
    elif args.task_cmd == "list":
        for t in orch.store.list_tasks():
            print(f"{t.id}  {t.status.value:<9} {t.type}/{t.difficulty}  runs={t.runs_count}  {t.title}")
    elif args.task_cmd == "show":
        if args.id == "latest":
            all_tasks = orch.store.list_tasks()
            args.id = all_tasks[-1].id if all_tasks else "T-0000"
        t = orch.store.get_task(args.id)
        if not t:
            print(f"没有 {args.id}")
            return 1
        route = orch.router.route(t)
        print(f"{t.id}: {t.title}\n状态: {t.status.value}   类型: {t.type}/{t.difficulty}   风险: {t.risk}")
        print(f"路由: {' -> '.join(route)}   当前级别: {route[t.ladder] if t.ladder < len(route) else '（已越过最后一级）'}")
        print(f"验收: {t.verify}\n范围: {t.scope}\n依赖: {t.depends_on}")
        usd, tokens = orch.task_spend(t.id)
        print(f"worktree: {t.worktree or '-'}\n备注: {t.note or '-'}")
        print(f"花费: API ${usd:.4f}，token {tokens:,}（含审查；上限见 [budget]）\n")
        for r in orch.store.runs_for(t.id):
            cost = f"${r.est_cost_usd:.3f}" if r.est_cost_usd else "-"
            if r.kind == "review":
                outcome = {True: "approved", False: "changes", None: "-"}[r.verified]
                print(f"  {r.id} {_fmt_time(r.started_at)} {r.pool:<12} {r.status.value:<12} "
                      f"review={outcome} cost={cost}")
            else:
                print(f"  {r.id} {_fmt_time(r.started_at)} {r.pool:<12} {r.status.value:<12} "
                      f"verified={r.verified} resumed={r.resumed} cost={cost}")
            if r.error:
                print(f"       error: {r.error.strip().splitlines()[-1][:120]}")
        review = orch.cfg.agents_dir / "tasks" / t.id / "REVIEW.md"
        if review.exists():
            print("\n" + review.read_text(encoding="utf-8").strip())
        print(f"\n文件: {orch.cfg.agents_dir / 'tasks' / t.id}")
    elif args.task_cmd == "retry":
        t = orch.store.get_task(args.id)
        if not t:
            print(f"没有 {args.id}")
            return 1
        t.status, t.runs_count, t.verify_failures, t.note = TaskStatus.QUEUED, 0, 0, "retry requested"
        t.review_rounds = 0
        if args.from_start:
            t.ladder = 0
        orch.store.save_task(t)
        print(f"{t.id} 已重新排队（级别 {t.ladder}）")
    elif args.task_cmd == "cancel":
        from . import workspace as ws

        for tid in args.ids:
            t = orch.store.get_task(tid)
            if not t:
                print(f"没有 {tid}")
                continue
            if t.status in (TaskStatus.RUNNING, TaskStatus.MERGED):
                print(f"{tid} 状态是 {t.status.value}，不能取消")
                continue
            if t.worktree and Path(t.worktree).exists():
                ws.remove_worktree(orch.project, Path(t.worktree))  # the branch is kept
            t.status, t.note = TaskStatus.CANCELLED, "cancelled by user"
            orch.store.save_task(t)
            orch.store.log("task_cancelled", "", task_id=tid)
            print(f"{tid} 已取消: {t.title}")
    elif args.task_cmd == "accept":
        t = orch.store.get_task(args.id)
        if not t:
            print(f"没有 {args.id}")
            return 1
        work = orch.store.runs_for(t.id, kind="work")
        if t.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED) or not work or work[-1].verified is not True:
            print(f"{t.id} 状态是 {t.status.value}，最后一次运行验收{'通过' if work and work[-1].verified else '未通过'}。"
                  "只有验收通过、但被审查卡住的任务可以人工放行。")
            return 1
        t.status, t.note = TaskStatus.VERIFIED, "accepted by a human despite the review"
        orch.store.save_task(t)
        orch.store.log("task_accepted", t.note, task_id=t.id)
        print(f"{t.id} 已人工放行。合并: python -m orch -p \"{orch.project}\" merge {t.id}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    orch = _open(args)
    try:
        orch.run(only=args.ids or None, max_tasks=args.max_tasks, auto_merge=args.auto_merge, jobs=args.jobs)
    except KeyboardInterrupt:
        print("\n已停止。正在运行的任务已做 checkpoint，下次 orch run 会带着交接说明继续。")
        return 130
    return cmd_status(args)


def cmd_status(args: argparse.Namespace) -> int:
    orch = _open(args)
    tasks = orch.store.list_tasks()
    print("\n== 任务 ==")
    if not tasks:
        print("（无）")
    for t in tasks:
        runs = orch.store.runs_for(t.id)
        work = [r for r in runs if r.kind == "work"]
        last = work[-1].pool if work else "-"
        reviews = len(runs) - len(work)
        extra = f"+{reviews}审" if reviews else ""
        note = f"  ({t.note[:90]})" if t.note else ""
        print(f"{t.id}  {t.status.value:<9} last={last:<12} runs={len(work)}{extra}  {t.title}{note}")
    print("\n== 额度池 ==")
    now = time.time()
    breakers = {r["pool"]: r for r in orch.store.all_breakers()}
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    usage = {r["pool"]: r for r in orch.store.usage_since(midnight)}
    for pool, pcfg in orch.cfg.pools.items():
        b = breakers.get(pool)
        state = "open until " + _fmt_time(b["open_until"]) if b and b["open_until"] > now else "closed"
        u = usage.get(pool)
        today = f"today runs={u['runs']} cost=${u['cost'] or 0:.3f} tokens={u['tin'] or 0}/{u['tout'] or 0}" if u else "today -"
        print(f"{pool:<12} {pcfg.get('executor', '?'):<12} breaker={state:<20} {today}")
        quota = orch.store.latest_quota(pool)
        if quota:
            names = {"five_hour": "5小时", "seven_day": "7天"}
            parts = [
                f"{names.get(k, k)} {w['utilization']:.0%}（{_fmt_time(w.get('resets_at'))} 重置）"
                for k, w in quota.items()
            ]
            print(f"{'':<12} 额度: " + " · ".join(parts))
    print("\n== 最近事件 ==")
    for r in reversed(orch.store.tail_log(8)):
        print(f"{_fmt_time(r['ts'])} {r['task_id'] or '':<7} {r['kind']:<14} {r['message'][:90]}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import json

    from .report import build_report, format_report, parse_since

    orch = _open(args)
    try:
        since = parse_since(args.since)
    except ValueError as e:
        raise OrchError(str(e)) from None
    executors = {pool: str(pcfg.get("executor", "")) for pool, pcfg in orch.cfg.pools.items()}
    rep = build_report(orch.store, orch.cfg.agents_dir, executors, since=since, task_type=args.type)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_report(rep), end="")
    return 0


def _print_manifest(run, m: dict) -> None:
    budget = f" / 预算 {m['budget']:,}（{m['class']}）" if m.get("budget") else "（只测量，不设预算）"
    print(f"\n{run.id}  {run.task_id}  {run.kind}  pool={run.pool}  mode={m.get('mode')}")
    print(f"  注入合计 {m['total']:,} tokens{budget}" + ("  ** 超预算 **" if m.get("over_budget") else ""))
    if run.tokens_in:
        share = m["total"] / run.tokens_in
        print(f"  执行者报告的输入 {run.tokens_in:,} tokens，其中 orch 注入约占 {share:.0%}"
              "（其余是执行壳自己的系统提示、工具定义和它自己打开的文件）")
    for sec in m["sections"]:
        print(f"    {sec['name']:<18} {sec['tokens']:>7,}  {sec['kind']}")
    print(f"  项目记忆 {m.get('memory_entries', 0)} 条；原始历史注入: {'是' if m.get('raw_history_injected') else '否'}")
    for r in m.get("retrieved", []):
        score = "核心" if r["score"] is None else f"{r['score']}分"
        mode = {"full": "全文", "pointer": "指针", "dropped": "丢弃"}.get(r["mode"], r["mode"])
        print(f"    [{mode}] {r['key']:<16} {r['kind']:<12} {score:<5} {r['tokens']:>5}  {'; '.join(r['reasons'])}")
    for c in m.get("compression", []):
        print(f"    压缩: {c['key']} -> {c['step']}")


def cmd_context(args: argparse.Namespace) -> int:
    import json

    orch = _open(args)
    if args.title:  # preview: what a task like this would be given, without running anything
        import tempfile

        from . import context as ctxm
        from . import handoff as ho
        from .models import Run

        task = Task(id="T-PREVIEW", title=args.title, spec=args.spec or "", type=args.type,
                    difficulty=args.difficulty, scope=args.scope or [], verify=args.verify or [])
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            ho.write_task_files(wt, task, "fresh")
            m = ctxm.build(orch, task, wt, "fresh")
            _print_manifest(Run(id="(预览)", task_id=task.id, pool="-", executor="-"), m)
            if args.show:
                print("\n" + (wt / ".task" / "CONTEXT.md").read_text(encoding="utf-8"))
        return 0
    target = args.id
    if target == "latest":
        rows = [r for r in orch.store.recent_runs(50) if r.context_manifest]
        runs = rows[:1]
    elif target.startswith("R-"):
        runs = [r for r in [orch.store.get_run(target)] if r]
    else:
        runs = orch.store.runs_for(target)
    runs = [r for r in runs if r.context_manifest]
    if not runs:
        print(f"{target}：没有带上下文记录的运行（V2-1 之前的运行没有记录）")
        return 1
    for r in runs:
        _print_manifest(r, json.loads(r.context_manifest))
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    from . import memory

    orch = _open(args)
    with orch.git_lock:
        if args.action == "init":
            created, sha = memory.init(orch)
            print(f"已创建 {', '.join(created) or '（都已存在）'}" + (f"，已提交 {sha}" if sha else ""))
            print(f"请编辑 {orch.cfg.worktrees_dir / '_integration' / memory.MEMORY_DIR} 下的文件填写目标、约束、架构，"
                  "然后运行 orch memory commit")
        elif args.action == "commit":
            sha = memory.commit_human_edits(orch)
            print(f"已提交 {sha}" if sha else "没有改动")
        else:
            entries = memory.load(orch)
            if not entries:
                print("还没有项目记忆（orch memory init 创建）")
            for e in entries:
                tags = f"  {{paths: {', '.join(e.paths)}}}" if e.paths else ""
                print(f"{e.key:<16} {e.kind:<12} {e.text}{tags}")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    """v0.3: check a direct-API pool: key present, models listed, one tiny call."""
    from . import llm
    from .adapters import make_adapter

    orch = _open(args)
    try:
        adapter = make_adapter(orch.cfg, args.pool)
    except KeyError as e:
        raise OrchError(str(e)) from None
    if adapter.executor != "api":
        raise OrchError(f"{args.pool} 不是直接 API 池（executor = {adapter.executor}）")
    env = adapter._cfg("api_key_env")
    key = adapter._key()
    if not key:
        print(f"没有 API key：设置环境变量 {env}，或者运行 orch key set。没有 key 时这个池会被自动跳过。")
        return 1
    base = adapter._cfg("base_url")
    print(f"池 {args.pool}：{base}，模型 {adapter.model}，公司 {adapter.vendor}（key 不显示）")
    try:
        models = llm.list_models(base, key, headers=adapter._headers(key))
        print("可用模型: " + (", ".join(models) or "（接口没有返回列表）"))
        if models and adapter.model not in models:
            print(f"注意：配置的模型 {adapter.model} 不在列表里，请改 [pools.{args.pool}] model")
        reply = llm.chat(base, key, adapter.model, [{"role": "user", "content": "Reply with the single word OK."}],
                         timeout=60, max_tokens=5, headers=adapter._headers(key))
    except llm.LLMError as e:
        print(f"调用失败（{e.kind}）：{e}")
        return 1
    cost = llm.cost_usd(reply, adapter.pool_cfg)
    print(f"测试调用成功：回答 {reply.text.strip()[:20]!r}，输入 {reply.tokens_in} / 输出 {reply.tokens_out} token"
          + (f"，约 ${cost:.6f}" if cost is not None else "（没配价格，只记 token）"))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from .report import rescan_usage

    orch = _open(args)
    changed = rescan_usage(orch)
    for rid, old, new in changed:
        print(f"{rid}: tokens_in {old} -> {new}")
    print(f"重算完成：{len(changed)} 次运行的用量有变化（含缓存命中和调用轮数）")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    import tomllib

    from . import advice

    orch = _open(args)
    if args.action == "apply":
        if not args.id:
            raise OrchError("用法：orch route apply S-1")
        try:
            backup, line = advice.apply(orch, args.id)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise OrchError(str(e)) from None
        print(f"已写入 .agents/orch.toml：{line}\n原文件备份在 {backup}")
        return 0
    items = advice.suggest(orch, args.min)
    if not items:
        print("还没有执行记录，没有建议。")
    marks = {"drop-first": "建议", "keep": "保持", "wait": "观察"}
    for a in items:
        head = f"[{a.id}] " if a.id else ""
        print(f"{marks[a.action]}  {head}{a.reason}")
        print(f"      现在: {a.key} = {' -> '.join(a.route)}")
        if a.id:
            print(f"      改成: {a.toml_line()}    （同意就运行 orch route apply {a.id}）")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp

    return mcp.main(args.projects or [str(_project(args))])


def cmd_key(args: argparse.Namespace) -> int:
    from . import connect

    try:
        if args.action == "list":
            print("\n".join(connect.key_list()))
        elif not args.provider:
            raise OrchError("which provider? e.g. orch key set deepseek")
        elif args.action == "set":
            print(connect.key_set(args.provider))
        else:
            print(connect.key_remove(args.provider))
    except ValueError as e:
        raise OrchError(str(e)) from None
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    from . import connect

    cfg = Config.load(_project(args))
    try:
        lines = connect.connect(cfg, args.provider, args.purpose, args.model or "") if args.provider \
            else connect.status(cfg)
    except ValueError as e:
        raise OrchError(str(e)) from None
    print("\n".join(lines))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    orch = _open(args)
    from . import checkpoint

    with orch.lock():
        ok = orch.merge(args.id, keep_worktree=args.keep_worktree)
        checkpoint.refresh(orch)
        return 0 if ok else 1


def cmd_handoff(args: argparse.Namespace) -> int:
    from . import checkpoint

    orch = _open(args)
    path, sha = checkpoint.export(orch, Path(args.out) if args.out else None, commit=not args.no_commit)
    print(f"已写入 {path}" + (f"，并提交到 {orch.integration_branch}: docs/project/{checkpoint.CHECKPOINT_FILE}（{sha}）" if sha else ""))
    return 0


def cmd_breaker(args: argparse.Namespace) -> int:
    orch = _open(args)
    if args.breaker_cmd == "open":
        if args.pool not in orch.cfg.pools:
            print(f"没有这个池: {args.pool}（可选: {', '.join(orch.cfg.pools)}）")
            return 1
        until = orch.breakers.force_open(args.pool, args.minutes)
        print(f"{args.pool} 已暂停到 {_fmt_time(until)}，期间任务会交给路由里的下一个池。")
        return 0
    orch.breakers.reset(args.pool)
    print("熔断器已重置" + (f": {args.pool}" if args.pool else "（全部）"))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    import json as _json

    from .plan import PlanError, Plan, approve, render, run_planner

    orch = _open(args)
    words = args.words
    sub = words[0] if words and words[0] in ("show", "approve", "reject", "list") else "new"

    def show(row) -> None:
        if row is None:
            print("还没有任何计划。用法: orch plan \"你的目标\"")
            return
        if not row["plan_json"]:
            print(f"{row['id']} [{row['status']}] {row['goal']}\n{row['error'] or ''}")
            return
        d = _json.loads(row["plan_json"])
        print(render(row["id"], row["goal"], Plan(d["summary"], d["tasks"], d.get("warnings", [])), row["status"]))

    try:
        if sub == "list":
            for r in orch.store.list_plans():
                print(f"{r['id']}  {r['status']:<9} {r['goal'][:70]}")
            return 0
        if sub == "show":
            show(orch.store.get_plan(words[1]) if len(words) > 1 else orch.store.latest_plan())
            return 0
        if sub in ("approve", "reject"):
            if len(words) < 2:
                print(f"用法: orch plan {sub} P-0001")
                return 1
            pid = words[1]
            if pid == "latest":
                latest = orch.store.latest_plan()
                if latest is None:
                    print("还没有任何计划")
                    return 1
                pid = latest["id"]
            if sub == "reject":
                orch.store.set_plan_status(pid, "rejected")
                print(f"{pid} 已放弃")
                return 0
            tasks = approve(orch, pid)
            from . import checkpoint

            checkpoint.refresh(orch)
            for t in tasks:
                deps = f"  (依赖 {', '.join(t.depends_on)})" if t.depends_on else ""
                print(f"已添加 {t.id}: {t.title}  -> {' -> '.join(orch.router.route(t))}{deps}")
            print(f"\n下一步: python -m orch -p \"{orch.project}\" run --auto-merge")
            return 0
        goal = " ".join(words).strip()
        if not goal:
            print("用法: orch plan \"你的目标\"")
            return 1
        context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
        with orch.lock():
            pid, plan = run_planner(orch, goal, context, pool=args.pool)
        show(orch.store.get_plan(pid))
        print(f"确认无误: python -m orch -p \"{orch.project}\" plan approve {pid}")
        print(f"放弃重来: python -m orch -p \"{orch.project}\" plan reject {pid}")
        return 0
    except PlanError as e:
        print(f"计划失败: {e}")
        return 1


def cmd_decide(args: argparse.Namespace) -> int:
    from .decide import DecideError, run_decide, set_status

    orch = _open(args)
    words = args.words
    sub = words[0] if words and words[0] in ("show", "accept", "reject", "list") else "new"

    def resolve(ref: str | None):
        if ref in (None, "latest"):
            return orch.store.latest_decision()
        return orch.store.get_decision(ref)

    try:
        if sub == "list":
            for r in orch.store.list_decisions():
                print(f"{r['id']}  {r['status']:<9} {(r['title'] or r['question'])[:70]}")
            return 0
        if sub == "show":
            row = resolve(words[1] if len(words) > 1 else None)
            if row is None:
                print("还没有任何决策。用法: orch decide \"问题\"")
                return 1
            if row["adr_path"] and Path(row["adr_path"]).exists():
                print(Path(row["adr_path"]).read_text(encoding="utf-8"))
            else:
                print(f"{row['id']} [{row['status']}] {row['question']}\n{row['error'] or ''}")
            print(f"\n文件: {orch.cfg.agents_dir / 'decisions' / row['id']}（方案、互评、每次调用的提示词）")
            return 0
        if sub in ("accept", "reject"):
            row = resolve(words[1] if len(words) > 1 else None)
            if row is None:
                print(f"用法: orch decide {sub} D-0001")
                return 1
            with orch.lock():
                where = set_status(orch, row["id"], "accepted" if sub == "accept" else "rejected")
                from . import checkpoint

                checkpoint.refresh(orch)
            print(f"{row['id']} 已标记为 {'accepted' if sub == 'accept' else 'rejected'}" + (f"，已提交 {where}" if where else ""))
            return 0
        question = " ".join(words).strip()
        if not question:
            print("用法: orch decide \"问题\"")
            return 1
        context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
        with orch.lock():
            did = run_decide(orch, question, context, language=args.lang)
        row = orch.store.get_decision(did)
        print("\n" + Path(row["adr_path"]).read_text(encoding="utf-8"))
        print(f"采纳: python -m orch -p \"{orch.project}\" decide accept {did}")
        print(f"否决: python -m orch -p \"{orch.project}\" decide reject {did}")
        return 0
    except DecideError as e:
        print(f"决策失败: {e}")
        return 1


def cmd_handoff_test(args: argparse.Namespace) -> int:
    from .demo import create_handoff_test

    root = create_handoff_test(Path(args.dir))
    py = Path(sys.executable).name
    print(f"交接测试项目已创建: {root}")
    print("任务 T-0001「实现 textstats.py」的路由是 claude_sub -> codex_sub。步骤:")
    print(f'  1. {py} -m orch -p "{root}" run')
    print("     看到 R-0001 -> claude_sub 之后等 15 秒左右，按 Ctrl+C 打断 Claude")
    print(f'  2. {py} -m orch -p "{root}" breaker open claude_sub --minutes 60')
    print(f'  3. {py} -m orch -p "{root}" run')
    print("     这次 Codex 会在同一个 worktree 里带着 HANDOFF.md 接着做")
    print(f'  4. {py} -m orch -p "{root}" task show T-0001')
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import create_demo

    root = create_demo(Path(args.dir))
    py = Path(sys.executable).name
    print(f"演示项目已创建: {root}")
    print("里面有两个任务:")
    print("  T-0001 修复 add()     先派给“偷懒”的假执行者 -> 验收失败两次 -> 升级 -> 交接给好的执行者")
    print("  T-0002 实现 multiply() 先派给会被限流的假执行者 -> 熔断 -> 带交接说明换执行者")
    print("  两个任务验收通过后，都会先由“另一家公司”的假审查者只读审查，再合并")
    print("\n运行:")
    print(f'  {py} -m orch -p "{root}" run --auto-merge')
    print(f'  {py} -m orch -p "{root}" task show T-0002')
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .demo import run_probe

    return run_probe(args.executor, args.model or "", Path(args.dir), args.timeout)


# -- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="MAO multi-agent orchestrator (v0.1-pre)")
    p.add_argument("-p", "--project", default=".", help="项目目录（默认当前目录）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化项目").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="环境检查").set_defaults(fn=cmd_doctor)

    t = sub.add_parser("task", help="任务管理")
    tsub = t.add_subparsers(dest="task_cmd", required=True)
    ta = tsub.add_parser("add", help="添加任务")
    ta.add_argument("-f", "--file", help="从 TOML 文件读取任务")
    ta.add_argument("--title")
    ta.add_argument("--spec")
    ta.add_argument("--spec-file")
    ta.add_argument("--type", default="code_change")
    ta.add_argument("--difficulty", default="M", choices=["S", "M", "L"])
    ta.add_argument("--risk", default="normal", choices=["low", "normal", "high", "critical"])
    ta.add_argument("--scope", action="append", help="可多次指定")
    ta.add_argument("--verify", action="append", help="验收命令，可多次指定")
    ta.add_argument("--depends", action="append", help="依赖的任务 id")
    ta.add_argument("--max-runs", type=int)
    ta.add_argument("--review", default="auto", choices=["auto", "always", "never"],
                    help="LLM 审查：auto = 按配置（默认只审 risk>=high 或没有验收命令的）")
    ta.add_argument("--allow-duplicate", action="store_true", help="允许和已有任务同名")
    tsub.add_parser("list")
    ts = tsub.add_parser("show")
    ts.add_argument("id", help="任务 id，或 latest")
    tr = tsub.add_parser("retry", help="把 blocked/failed 的任务重新排队")
    tr.add_argument("id")
    tr.add_argument("--from-start", action="store_true", help="从路由的第一级重新开始")
    tc = tsub.add_parser("cancel", help="取消排队中 / blocked / failed 的任务（task retry 可以恢复）")
    tc.add_argument("ids", nargs="+")
    tac = tsub.add_parser("accept", help="验收已通过、但审查一直不通过的任务：人工放行")
    tac.add_argument("id")
    t.set_defaults(fn=cmd_task)

    r = sub.add_parser("run", help="运行排队中的任务")
    r.add_argument("ids", nargs="*")
    r.add_argument("--max-tasks", type=int)
    r.add_argument("--auto-merge", action="store_true", help="验收和审查都通过后自动合并")
    r.add_argument("--jobs", type=int, help="同时跑几个任务（默认看 [parallel] max_jobs；1 = 一个接一个）")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("status", help="状态总览").set_defaults(fn=cmd_status)

    rp = sub.add_parser("report", help="执行记录报表（类型 × 池、池 × 角色、任务明细）")
    rp.add_argument("--since", help="只看这之后的运行：7d 或 2026-10-01")
    rp.add_argument("--type", help="只看某种任务类型，例如 code_change")
    rp.add_argument("--json", action="store_true", help="输出 JSON")
    rp.set_defaults(fn=cmd_report)

    cx = sub.add_parser("context", help="查看某次运行实际注入的上下文（各部分 token、预算、检索、压缩）")
    cx.add_argument("id", nargs="?", default="latest", help="T-xxxx（该任务所有运行）、R-xxxx 或 latest")
    cx.add_argument("--title", help="预览：给出任务标题，不运行任何模型，只看会注入什么")
    cx.add_argument("--spec", default="")
    cx.add_argument("--type", default="code_change")
    cx.add_argument("--difficulty", default="M", choices=["S", "M", "L"])
    cx.add_argument("--scope", action="append")
    cx.add_argument("--verify", action="append")
    cx.add_argument("--show", action="store_true", help="预览时打印 CONTEXT.md 全文")
    cx.set_defaults(fn=cmd_context)

    me = sub.add_parser("memory", help="项目记忆（docs/project/）：init 创建 | show 查看 | commit 提交手工修改")
    me.add_argument("action", choices=["init", "show", "commit"], nargs="?", default="show")
    me.set_defaults(fn=cmd_memory)

    m = sub.add_parser("merge", help="合并已验收的任务")
    m.add_argument("id")
    m.add_argument("--keep-worktree", action="store_true")
    m.set_defaults(fn=cmd_merge)

    b = sub.add_parser("breaker", help="熔断器")
    bsub = b.add_subparsers(dest="breaker_cmd", required=True)
    br = bsub.add_parser("reset")
    br.add_argument("pool", nargs="?")
    bo = bsub.add_parser("open", help="手动暂停一个池（例如把任务从 Claude 切给 Codex）")
    bo.add_argument("pool")
    bo.add_argument("--minutes", type=float, default=60)
    b.set_defaults(fn=cmd_breaker)

    pl = sub.add_parser("plan", help='M4：让 Manager 把目标拆成任务。plan "目标" | show | approve ID | reject ID | list')
    pl.add_argument("words", nargs="*")
    pl.add_argument("--context-file", help="给 Manager 的补充说明（文本文件）")
    pl.add_argument("--pool", help="指定用哪个池做规划（默认按 [routing] 的 plan 规则）")
    pl.set_defaults(fn=cmd_plan)

    dc = sub.add_parser("decide", help='M5：两家出方案、互评、裁判写 ADR。decide "问题" | show | accept ID | reject ID | list')
    dc.add_argument("words", nargs="*")
    dc.add_argument("--context-file", help="补充说明（文本文件）")
    dc.add_argument("--lang", help='回答用的语言，例如 "Simplified Chinese"（默认看 [decide] language）')
    dc.set_defaults(fn=cmd_decide)

    ky = sub.add_parser("key", help="save your own API keys (encrypted on Windows): key set|list|remove PROVIDER")
    ky.add_argument("action", choices=["set", "list", "remove"])
    ky.add_argument("provider", nargs="?", help="deepseek, openai, anthropic, qwen, gemini, doubao")
    ky.set_defaults(fn=cmd_key)

    cn = sub.add_parser("connect", help="show which models you can use, or add one to this project: connect [PROVIDER]")
    cn.add_argument("provider", nargs="?")
    cn.add_argument("--for", dest="purpose", choices=["coding", "review"], default="coding")
    cn.add_argument("--model", help="model to use (coding: provider/model as opencode names it)")
    cn.set_defaults(fn=cmd_connect)

    mc = sub.add_parser("mcp", help="MCP 服务（stdio）：在 Claude / ChatGPT 等聊天应用里查看和派发 orch 任务")
    mc.add_argument("--project", dest="projects", action="append", help="要管理的项目目录，可多次指定（默认 -p 的项目）")
    mc.set_defaults(fn=cmd_mcp)

    ro = sub.add_parser("route", help="v0.3：按执行记录给路由建议（suggest），你同意后再改配置（apply S-n）")
    ro.add_argument("action", choices=["suggest", "apply"])
    ro.add_argument("id", nargs="?")
    ro.add_argument("--min", type=int, help="给建议所需的最少任务数（默认 5）")
    ro.set_defaults(fn=cmd_route)

    us = sub.add_parser("usage", help="v0.3：从保存的事件流重算历史运行的 token（含缓存命中、调用轮数）。usage rescan")
    us.add_argument("action", choices=["rescan"])
    us.set_defaults(fn=cmd_usage)

    ap = sub.add_parser("api", help="v0.3：检查直接 API 池（key、模型列表、一次极小的调用）。api check deepseek_api")
    ap.add_argument("action", choices=["check"])
    ap.add_argument("pool", nargs="?", default="deepseek_api")
    ap.set_defaults(fn=cmd_api)

    hx = sub.add_parser("handoff", help="V2-7：生成项目交接文件（Checkpoint），给新的 AI 会话读。handoff export")
    hx.add_argument("action", choices=["export"])
    hx.add_argument("--out", help="写到这个文件（默认 .agents/PROJECT_HANDOFF.md）")
    hx.add_argument("--no-commit", action="store_true", help="不提交到 integration 分支的 docs/project/CHECKPOINT.md")
    hx.set_defaults(fn=cmd_handoff)

    h = sub.add_parser("handoff-test", help="创建真实交接测试项目（Claude -> Codex）")
    h.add_argument("dir", nargs="?", default="playground/handoff")
    h.set_defaults(fn=cmd_handoff_test)

    d = sub.add_parser("demo", help="创建离线演示项目")
    d.add_argument("dir", nargs="?", default="playground/demo")
    d.set_defaults(fn=cmd_demo)

    pr = sub.add_parser("probe", help="M0：用真实 CLI 跑一个极小任务")
    pr.add_argument("executor", choices=["claude_code", "codex", "opencode"])
    pr.add_argument("--model", default="")
    pr.add_argument("--dir", default="probe")
    pr.add_argument("--timeout", type=float, default=10, help="分钟")
    pr.set_defaults(fn=cmd_probe)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更新版本", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except OrchError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    finally:
        while _OPENED:
            _OPENED.pop().store.close()
