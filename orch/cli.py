"""Command line: python -m orch [-p PROJECT] <command> ...

Commands
  init            初始化项目（.agents/、配置、integration 分支）
  doctor          检查 Python / git / 各 CLI 是否可用
  task add|list|show|retry|accept|cancel
  plan "目标"     M4：Manager 只读分析后拆成任务；plan approve P-0001 确认后才会建任务
  decide "问题"   M5：两家各出方案、互相点评、裁判写决策记录（ADR）；decide accept|reject D-0001
  run             运行排队中的任务
  status          任务、熔断器、今日用量
  sessions        本机的 Claude Code / Codex 会话（包括你手动开的）：时间、项目、用量；只读，不看对话内容
  agents [pause|resume|swap]   总控面板：每个 agent 在做什么、用量、成功率、最近报错；暂停 / 恢复 / 换人
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
from . import i18n
from .i18n import tr


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
    print(tr("完成。下一步: 编辑 .agents/orch.toml，然后 orch doctor"))
    return 0


INSTALL_HINTS = {
    # Windows（PowerShell）; 装完后要开一个新终端，PATH 才会生效
    "git": "winget install Git.Git",
    "claude": tr("irm https://claude.ai/install.ps1 | iex   或   winget install Anthropic.ClaudeCode"),
    "codex": tr("npm install -g @openai/codex   （需要 Node.js: winget install OpenJS.NodeJS.LTS）"),
    "opencode": tr("npm install -g opencode-ai   （需要 Node.js）"),
}


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"OS      : {platform.system()} {platform.release()} ({platform.machine()})")
    py = sys.version_info
    py_ok = py >= (3, 11)
    ok &= py_ok
    print(f"Python  : {platform.python_version()} {'OK' if py_ok else tr('需要 3.11 以上')}")
    code, out = run_capture(["git", "--version"])
    ok &= code == 0
    print(f"git     : {out if code == 0 else tr('未找到') + '  -> ' + INSTALL_HINTS['git']}")
    project = _project(args)
    cfg = Config.load(project)
    seen: set[str] = set()
    print(tr("\n执行壳（按 orch.toml 的 [pools]）:"))
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
        line = f"  {pool:<12} {ex:<12} {cmd} -> {path or tr('未找到')}"
        if not path and cmd in INSTALL_HINTS:
            line += tr("\n               安装: {0}", INSTALL_HINTS[cmd])
        if path and cmd not in seen:
            seen.add(cmd)
            vcode, vout = run_capture([cmd, "--version"], timeout=30)
            line += f"   [{(vout.splitlines() or ['?'])[0][:60]}]"
        print(line)
    if (project / ".agents").is_dir():
        print(tr("\n项目    : {0}（已初始化）  worktrees -> {1}", project, cfg.worktrees_dir))
    else:
        print(tr("\n项目    : {0}（未初始化，运行 orch init）", project))
    return 0 if ok else 1


def _task_from_args(args: argparse.Namespace) -> Task:
    if args.file:
        with open(args.file, "rb") as f:
            data = tomllib.load(f)
        data.pop("id", None)
        data.pop("status", None)
        return Task(id="", **data)
    if not args.title:
        raise SystemExit(tr("需要 --title（或用 -f task.toml）"))
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
            print(tr("已经有同名任务 {0}（{1}），没有重复添加。确实要再加一个，请带上 --allow-duplicate", same[-1].id, same[-1].status.value))
            return 1
        task = orch.store.add_task(task)
        route = orch.router.route(task)
        print(tr("已添加 {0}: {1}  路由: {2}", task.id, task.title, ' -> '.join(route) or tr('（无匹配规则！）')))
        if not task.verify:
            print(tr("提醒: 没有 --verify 验收命令，完成与否只能听 agent 自己说。"))
    elif args.task_cmd == "list":
        for t in orch.store.list_tasks():
            print(f"{t.id}  {t.status.value:<9} {t.type}/{t.difficulty}  runs={t.runs_count}  {t.title}")
    elif args.task_cmd == "show":
        if args.id == "latest":
            all_tasks = orch.store.list_tasks()
            args.id = all_tasks[-1].id if all_tasks else "T-0000"
        t = orch.store.get_task(args.id)
        if not t:
            print(tr("没有 {0}", args.id))
            return 1
        route = orch.router.route(t)
        print(tr("{0}: {1}\n状态: {2}   类型: {3}/{4}   风险: {5}", t.id, t.title, t.status.value, t.type, t.difficulty, t.risk))
        print(tr("路由: {0}   当前级别: {1}", ' -> '.join(route), route[t.ladder] if t.ladder < len(route) else tr('（已越过最后一级）')))
        print(tr("验收: {0}\n范围: {1}\n依赖: {2}", t.verify, t.scope, t.depends_on))
        usd, tokens = orch.task_spend(t.id)
        print(tr("worktree: {0}\n备注: {1}", t.worktree or '-', t.note or '-'))
        print(tr("花费: API ${0:.4f}，token {1:,}（含审查；上限见 [budget]）\n", usd, tokens))
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
        print(tr("\n文件: {0}", orch.cfg.agents_dir / 'tasks' / t.id))
    elif args.task_cmd == "retry":
        t = orch.store.get_task(args.id)
        if not t:
            print(tr("没有 {0}", args.id))
            return 1
        t.status, t.runs_count, t.verify_failures, t.note = TaskStatus.QUEUED, 0, 0, "retry requested"
        t.review_rounds = 0
        if args.from_start:
            t.ladder = 0
        orch.store.save_task(t)
        print(tr("{0} 已重新排队（级别 {1}）", t.id, t.ladder))
    elif args.task_cmd == "cancel":
        from . import workspace as ws

        for tid in args.ids:
            t = orch.store.get_task(tid)
            if not t:
                print(tr("没有 {0}", tid))
                continue
            if t.status in (TaskStatus.RUNNING, TaskStatus.MERGED):
                print(tr("{0} 状态是 {1}，不能取消", tid, t.status.value))
                continue
            if t.worktree and Path(t.worktree).exists():
                ws.remove_worktree(orch.project, Path(t.worktree))  # the branch is kept
            t.status, t.note = TaskStatus.CANCELLED, "cancelled by user"
            orch.store.save_task(t)
            orch.store.log("task_cancelled", "", task_id=tid)
            print(tr("{0} 已取消: {1}", tid, t.title))
    elif args.task_cmd == "accept":
        t = orch.store.get_task(args.id)
        if not t:
            print(tr("没有 {0}", args.id))
            return 1
        work = orch.store.runs_for(t.id, kind="work")
        if t.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED) or not work or work[-1].verified is not True:
            print(tr("{0} 状态是 {1}，最后一次运行验收{2}。只有验收通过、但被审查卡住的任务可以人工放行。", t.id, t.status.value, tr('通过') if work and work[-1].verified else tr('未通过')))
            return 1
        t.status, t.note = TaskStatus.VERIFIED, "accepted by a human despite the review"
        orch.store.save_task(t)
        orch.store.log("task_accepted", t.note, task_id=t.id)
        print(tr("{0} 已人工放行。合并: python -m orch -p \"{1}\" merge {2}", t.id, orch.project, t.id))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    orch = _open(args)
    try:
        orch.run(only=args.ids or None, max_tasks=args.max_tasks, auto_merge=args.auto_merge, jobs=args.jobs)
    except KeyboardInterrupt:
        print(tr("\n已停止。正在运行的任务已做 checkpoint，下次 orch run 会带着交接说明继续。"))
        return 130
    return cmd_status(args)


def cmd_status(args: argparse.Namespace) -> int:
    orch = _open(args)
    tasks = orch.store.list_tasks()
    print(tr("\n== 任务 =="))
    if not tasks:
        print(tr("（无）"))
    for t in tasks:
        runs = orch.store.runs_for(t.id)
        work = [r for r in runs if r.kind == "work"]
        last = work[-1].pool if work else "-"
        reviews = len(runs) - len(work)
        extra = tr("+{0}审", reviews) if reviews else ""
        note = f"  ({t.note[:90]})" if t.note else ""
        print(f"{t.id}  {t.status.value:<9} last={last:<12} runs={len(work)}{extra}  {t.title}{note}")
    print(tr("\n== 额度池 =="))
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
            names = {"five_hour": tr("5小时"), "seven_day": tr("7天")}
            parts = [
                tr("{0} {1:.0%}（{2} 重置）", names.get(k, k), w['utilization'], _fmt_time(w.get('resets_at')))
                for k, w in quota.items()
            ]
            print(tr("{0:<12} 额度: ", '') + " · ".join(parts))
    print(tr("\n== 最近事件 =="))
    for r in reversed(orch.store.tail_log(8)):
        print(f"{_fmt_time(r['ts'])} {r['task_id'] or '':<7} {r['kind']:<14} {r['message'][:90]}")
    return 0


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "-"
    m = int((now - ts) // 60)
    if m < 1:
        return tr("刚刚")
    return tr("{0} 分钟前", m) if m < 60 else (tr("{0} 小时前", m // 60) if m < 1440 else _fmt_time(ts))


def cmd_agents(args: argparse.Namespace) -> int:
    import json

    from . import agents as ag

    orch = _open(args)
    try:
        if args.agents_cmd == "pause":
            until = ag.pause(orch, args.pool, args.minutes)
            print(tr("{0} 已暂停", args.pool) + (tr("到 {0}", _fmt_time(until)) if args.minutes else tr("，直到 agents resume"))
                  + tr("。正在跑的不会被打断，新任务会交给路由里的下一个。"))
            return 0
        if args.agents_cmd == "resume":
            ag.resume(orch, args.pool)
            print(tr("{0} 已恢复", args.pool))
            return 0
        if args.agents_cmd == "swap":
            ag.swap(orch, args.task, args.pool)
            print(tr("{0} 改由 {1} 先做，已重新排队。开始: orch run {2}", args.task, args.pool, args.task))
            return 0
    except ValueError as e:
        print(e)
        return 1
    p = ag.panel(orch)
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0
    now = p["at"]
    names = {"working": tr("工作中"), "idle": tr("空闲"), "paused": tr("已暂停"), "cooling": tr("冷却中")}
    for a in p["agents"]:
        state = names.get(a["state"], a["state"])
        if a["until"] and not a["until_forever"]:
            state += tr("（到 {0}）", _fmt_time(a['until']))
        print(f"\n{a['pool']}  [{a['executor']}{' ' + a['model'] if a['model'] else ''}]  {state}")
        for c in a["current"]:
            print(tr("  正在做: {0} {1}（{2}，{3}开始）", c['task'], c['title'][:60], c['kind'], _ago(c['started_at'], now)))
        for label, u in ((tr("今天"), a["today"]), (tr("近7天"), a["week"])):
            money = f" ${u['cost_usd']:.3f}" if a["billing"] == "api" else ""
            print(tr("  {0}: {1} 次{2}  token {3:,}/{4:,}", label, u['runs'], money, u['tokens_in'], u['tokens_out']))
        if a["quota"]:
            wn = {"five_hour": tr("5小时"), "seven_day": tr("7天")}
            print(tr("  额度: ") + " · ".join(tr("{0} {1:.0%}（{2} 重置）", wn.get(k, k), w['utilization'], _fmt_time(w.get('resets_at')))
                                          for k, w in a["quota"].items()))
        r = a["recent"]
        if r["runs"]:
            print(tr("  最近 {0} 次: 验收通过 {1} 次（{2:.0%}），上次 {3}", r['runs'], r['ok'], r['rate'], _ago(a['last_run_at'], now)))
        if a["last_error"]:
            e = a["last_error"]
            status = tr("验收没通过") if e["status"] == "verify_failed" else e["status"]
            print(tr("  最近出错: {0} {1} {2}  {3}", e['task'], status, _ago(e['at'], now), e['text'][:100]))
    m = p.get("manual")
    if m and m["summary"]:
        tools = {"claude_code": "Claude Code", "codex": "Codex"}
        print(tr("\n本机今天的会话（含你手动开的）: ") + " · ".join(
            tr("{0} {1} 个（手动 {2}，正在用 {3}），token {4:,}", tools.get(k, k), v['sessions'], v['manual'], v['active'], v['tokens_today'])
            for k, v in m["summary"].items()))
        for x in m["active"]:
            print(tr("  正在用: {0} {1}  token {2:,}", tools.get(x['tool'], x['tool']), Path(x['project']).name or '-', x['tokens_today']))
    if p["queued"]:
        print(tr("\n排队中: ") + ", ".join(f"{q['task']}→{q['next'] or tr('无')}{tr('（指定）') if q['pin'] else ''}"
                                     for q in p["queued"]))
    for t in p["attention"]:
        print(tr("要你处理: {0} {1} {2}  {3}", t['task'], t['status'], t['title'][:50], t['note'][:80]))
    print(tr("\n操作: agents pause|resume 池名 · agents swap 任务 池名 · task retry|cancel 任务"))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    import json

    from . import sessions

    data = sessions.scan(days=args.days)
    if not args.all:
        data["sessions"] = [s for s in data["sessions"] if s["source"] == "manual"]
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    now = data["at"]
    tools = {"claude_code": "Claude Code", "codex": "Codex"}
    if not data["sessions"]:
        print(tr("最近 {0:g} 天没有会话记录（找的是 {1} 和 {2}）", args.days, data['dirs']['claude_code'], data['dirs']['codex']))
    for s in data["sessions"][:args.limit]:
        mark = tr("● 正在用") if s["active"] else _ago(s["last_at"], now)
        src = "" if s["source"] == "manual" else "  [orch]"
        branch = f" ({s['branch']})" if s["branch"] else ""
        print(f"{tools.get(s['tool'], s['tool']):<11} {mark:<10} {s['project'] or '-'}{branch}{src}")
        print(tr("{0:<11} {1}  {2} 轮  token 输入 {3:,}（缓存 {4:,}） 输出 {5:,}  今天 {6:,}", '', s['model'] or '-', s['turns'], s['tokens']['in'], s['tokens']['cached'], s['tokens']['out'], s['tokens_today']))
    for k, v in data["summary"].items():
        print(tr("\n{0}: {1} 个会话，手动 {2} 个，正在用 {3} 个；今天 token {4:,}（手动 {5:,}）", tools.get(k, k), v['sessions'], v['manual'], v['active'], v['tokens_today'], v['tokens_today_manual']))
    q = data["codex_quota"]
    if q:
        names = {"five_hour": tr("5小时"), "seven_day": tr("7天")}
        print(tr("Codex 额度: ") + " · ".join(tr("{0} {1:.0%}（{2} 重置）", names.get(k, k), w['utilization'], _fmt_time(w.get('resets_at')))
                                         for k, w in q.items()))
    print(tr("\n只读了时间、项目文件夹和用量，没有读对话内容和登录文件。"))
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
    budget = tr(" / 预算 {0:,}（{1}）", m['budget'], m['class']) if m.get("budget") else tr("（只测量，不设预算）")
    print(f"\n{run.id}  {run.task_id}  {run.kind}  pool={run.pool}  mode={m.get('mode')}")
    print(tr("  注入合计 {0:,} tokens{1}", m['total'], budget) + (tr("  ** 超预算 **") if m.get("over_budget") else ""))
    if run.tokens_in:
        share = m["total"] / run.tokens_in
        print(tr("  执行者报告的输入 {0:,} tokens，其中 orch 注入约占 {1:.0%}（其余是执行壳自己的系统提示、工具定义和它自己打开的文件）", run.tokens_in, share))
    for sec in m["sections"]:
        print(f"    {sec['name']:<18} {sec['tokens']:>7,}  {sec['kind']}")
    print(tr("  项目记忆 {0} 条；原始历史注入: {1}", m.get('memory_entries', 0), tr('是') if m.get('raw_history_injected') else tr('否')))
    for r in m.get("retrieved", []):
        score = tr("核心") if r["score"] is None else tr("{0}分", r['score'])
        mode = {"full": tr("全文"), "pointer": tr("指针"), "dropped": tr("丢弃")}.get(r["mode"], r["mode"])
        print(f"    [{mode}] {r['key']:<16} {r['kind']:<12} {score:<5} {r['tokens']:>5}  {'; '.join(r['reasons'])}")
    for c in m.get("compression", []):
        print(tr("    压缩: {0} -> {1}", c['key'], c['step']))


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
            _print_manifest(Run(id=tr("(预览)"), task_id=task.id, pool="-", executor="-"), m)
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
        print(tr("{0}：没有带上下文记录的运行（V2-1 之前的运行没有记录）", target))
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
            print(tr("已创建 {0}", ', '.join(created) or tr('（都已存在）')) + (tr("，已提交 {0}", sha) if sha else ""))
            print(tr("请编辑 {0} 下的文件填写目标、约束、架构，然后运行 orch memory commit", orch.cfg.worktrees_dir / '_integration' / memory.MEMORY_DIR))
        elif args.action == "commit":
            sha = memory.commit_human_edits(orch)
            print(tr("已提交 {0}", sha) if sha else tr("没有改动"))
        else:
            entries = memory.load(orch)
            if not entries:
                print(tr("还没有项目记忆（orch memory init 创建）"))
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
        raise OrchError(tr("{0} 不是直接 API 池（executor = {1}）", args.pool, adapter.executor))
    env = adapter._cfg("api_key_env")
    key = adapter._key()
    if not key:
        print(tr("没有 API key：设置环境变量 {0}，或者运行 orch key set。没有 key 时这个池会被自动跳过。", env))
        return 1
    base = adapter._cfg("base_url")
    print(tr("池 {0}：{1}，模型 {2}，公司 {3}（key 不显示）", args.pool, base, adapter.model, adapter.vendor))
    try:
        models = llm.list_models(base, key, headers=adapter._headers(key))
        print(tr("可用模型: ") + (", ".join(models) or tr("（接口没有返回列表）")))
        if models and adapter.model not in models:
            print(tr("注意：配置的模型 {0} 不在列表里，请改 [pools.{1}] model", adapter.model, args.pool))
        reply = llm.chat(base, key, adapter.model, [{"role": "user", "content": "Reply with the single word OK."}],
                         timeout=60, max_tokens=5, headers=adapter._headers(key))
    except llm.LLMError as e:
        print(tr("调用失败（{0}）：{1}", e.kind, e))
        return 1
    cost = llm.cost_usd(reply, adapter.pool_cfg)
    print(tr("测试调用成功：回答 {0!r}，输入 {1} / 输出 {2} token", reply.text.strip()[:20], reply.tokens_in, reply.tokens_out)
          + (tr("，约 ${0:.6f}", cost) if cost is not None else tr("（没配价格，只记 token）")))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from .report import rescan_usage

    orch = _open(args)
    changed = rescan_usage(orch)
    for rid, old, new in changed:
        print(f"{rid}: tokens_in {old} -> {new}")
    print(tr("重算完成：{0} 次运行的用量有变化（含缓存命中和调用轮数）", len(changed)))
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    import tomllib

    from . import advice

    orch = _open(args)
    if args.action == "apply":
        if not args.id:
            raise OrchError(tr("用法：orch route apply S-1"))
        try:
            backup, line = advice.apply(orch, args.id)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise OrchError(str(e)) from None
        print(tr("已写入 .agents/orch.toml：{0}\n原文件备份在 {1}", line, backup))
        return 0
    items = advice.suggest(orch, args.min)
    if not items:
        print(tr("还没有执行记录，没有建议。"))
    marks = {"drop-first": tr("建议"), "keep": tr("保持"), "wait": tr("观察")}
    for a in items:
        head = f"[{a.id}] " if a.id else ""
        print(f"{marks[a.action]}  {head}{a.reason}")
        print(tr("      现在: {0} = {1}", a.key, ' -> '.join(a.route)))
        if a.id:
            print(tr("      改成: {0}    （同意就运行 orch route apply {1}）", a.toml_line(), a.id))
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
            raise OrchError(tr("which provider? e.g. orch key set deepseek"))
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


def cmd_lang(args: argparse.Namespace) -> int:
    if args.lang_to:
        i18n.save(args.lang_to)
        i18n.set_lang(args.lang_to)
        print(tr("orch's messages are now in {0} (saved for this user; MAO_LANG or --lang still win)",
                 i18n.NAMES[args.lang_to]))
    else:
        cur = i18n.current()
        print(tr("current language: {0} ({1}). Change it with: orch lang zh | en | ja", cur, i18n.NAMES[cur]))
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
    print(tr("已写入 {0}", path) + (tr("，并提交到 {0}: docs/project/{1}（{2}）", orch.integration_branch, checkpoint.CHECKPOINT_FILE, sha) if sha else ""))
    return 0


def cmd_breaker(args: argparse.Namespace) -> int:
    orch = _open(args)
    if args.breaker_cmd == "open":
        if args.pool not in orch.cfg.pools:
            print(tr("没有这个池: {0}（可选: {1}）", args.pool, ', '.join(orch.cfg.pools)))
            return 1
        until = orch.breakers.force_open(args.pool, args.minutes)
        print(tr("{0} 已暂停到 {1}，期间任务会交给路由里的下一个池。", args.pool, _fmt_time(until)))
        return 0
    orch.breakers.reset(args.pool)
    print(tr("熔断器已重置") + (f": {args.pool}" if args.pool else tr("（全部）")))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    import json as _json

    from .plan import PlanError, Plan, approve, render, run_planner

    orch = _open(args)
    words = args.words
    sub = words[0] if words and words[0] in ("show", "approve", "reject", "list") else "new"

    def show(row) -> None:
        if row is None:
            print(tr("还没有任何计划。用法: orch plan \"你的目标\""))
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
                print(tr("用法: orch plan {0} P-0001", sub))
                return 1
            pid = words[1]
            if pid == "latest":
                latest = orch.store.latest_plan()
                if latest is None:
                    print(tr("还没有任何计划"))
                    return 1
                pid = latest["id"]
            if sub == "reject":
                orch.store.set_plan_status(pid, "rejected")
                print(tr("{0} 已放弃", pid))
                return 0
            tasks = approve(orch, pid)
            from . import checkpoint

            checkpoint.refresh(orch)
            for t in tasks:
                deps = tr("  (依赖 {0})", ', '.join(t.depends_on)) if t.depends_on else ""
                print(tr("已添加 {0}: {1}  -> {2}{3}", t.id, t.title, ' -> '.join(orch.router.route(t)), deps))
            print(tr("\n下一步: python -m orch -p \"{0}\" run --auto-merge", orch.project))
            return 0
        goal = " ".join(words).strip()
        if not goal:
            print(tr("用法: orch plan \"你的目标\""))
            return 1
        context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
        with orch.lock():
            pid, plan = run_planner(orch, goal, context, pool=args.pool)
        show(orch.store.get_plan(pid))
        print(tr("确认无误: python -m orch -p \"{0}\" plan approve {1}", orch.project, pid))
        print(tr("放弃重来: python -m orch -p \"{0}\" plan reject {1}", orch.project, pid))
        return 0
    except PlanError as e:
        print(tr("计划失败: {0}", e))
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
                print(tr("还没有任何决策。用法: orch decide \"问题\""))
                return 1
            if row["adr_path"] and Path(row["adr_path"]).exists():
                print(Path(row["adr_path"]).read_text(encoding="utf-8"))
            else:
                print(f"{row['id']} [{row['status']}] {row['question']}\n{row['error'] or ''}")
            print(tr("\n文件: {0}（方案、互评、每次调用的提示词）", orch.cfg.agents_dir / 'decisions' / row['id']))
            return 0
        if sub in ("accept", "reject"):
            row = resolve(words[1] if len(words) > 1 else None)
            if row is None:
                print(tr("用法: orch decide {0} D-0001", sub))
                return 1
            with orch.lock():
                where = set_status(orch, row["id"], "accepted" if sub == "accept" else "rejected")
                from . import checkpoint

                checkpoint.refresh(orch)
            print(tr("{0} 已标记为 {1}", row['id'], 'accepted' if sub == 'accept' else 'rejected') + (tr("，已提交 {0}", where) if where else ""))
            return 0
        question = " ".join(words).strip()
        if not question:
            print(tr("用法: orch decide \"问题\""))
            return 1
        context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
        with orch.lock():
            did = run_decide(orch, question, context, language=args.lang)
        row = orch.store.get_decision(did)
        print("\n" + Path(row["adr_path"]).read_text(encoding="utf-8"))
        print(tr("采纳: python -m orch -p \"{0}\" decide accept {1}", orch.project, did))
        print(tr("否决: python -m orch -p \"{0}\" decide reject {1}", orch.project, did))
        return 0
    except DecideError as e:
        print(tr("决策失败: {0}", e))
        return 1


def cmd_handoff_test(args: argparse.Namespace) -> int:
    from .demo import create_handoff_test

    root = create_handoff_test(Path(args.dir))
    py = Path(sys.executable).name
    print(tr("交接测试项目已创建: {0}", root))
    print(tr("任务 T-0001「实现 textstats.py」的路由是 claude_sub -> codex_sub。步骤:"))
    print(f'  1. {py} -m orch -p "{root}" run')
    print(tr("     看到 R-0001 -> claude_sub 之后等 15 秒左右，按 Ctrl+C 打断 Claude"))
    print(f'  2. {py} -m orch -p "{root}" breaker open claude_sub --minutes 60')
    print(f'  3. {py} -m orch -p "{root}" run')
    print(tr("     这次 Codex 会在同一个 worktree 里带着 HANDOFF.md 接着做"))
    print(f'  4. {py} -m orch -p "{root}" task show T-0001')
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import create_demo

    root = create_demo(Path(args.dir))
    py = Path(sys.executable).name
    print(tr("演示项目已创建: {0}", root))
    print(tr("里面有两个任务:"))
    print(tr("  T-0001 修复 add()     先派给“偷懒”的假执行者 -> 验收失败两次 -> 升级 -> 交接给好的执行者"))
    print(tr("  T-0002 实现 multiply() 先派给会被限流的假执行者 -> 熔断 -> 带交接说明换执行者"))
    print(tr("  两个任务验收通过后，都会先由“另一家公司”的假审查者只读审查，再合并"))
    print(tr("\n运行:"))
    print(f'  {py} -m orch -p "{root}" run --auto-merge')
    print(f'  {py} -m orch -p "{root}" task show T-0002')
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .demo import run_probe

    return run_probe(args.executor, args.model or "", Path(args.dir), args.timeout)


# -- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="MAO multi-agent orchestrator")
    p.add_argument("-p", "--project", default=".", help=tr("项目目录（默认当前目录）"))
    p.add_argument("--lang", choices=list(i18n.LANGS), help=tr("language of the messages: zh, en or ja (this run only)"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help=tr("初始化项目")).set_defaults(fn=cmd_init)

    lg = sub.add_parser("lang", help=tr("show or set the language of orch's messages: lang [zh|en|ja]"))
    lg.add_argument("lang_to", nargs="?", choices=list(i18n.LANGS))
    lg.set_defaults(fn=cmd_lang)
    sub.add_parser("doctor", help=tr("环境检查")).set_defaults(fn=cmd_doctor)

    t = sub.add_parser("task", help=tr("任务管理"))
    tsub = t.add_subparsers(dest="task_cmd", required=True)
    ta = tsub.add_parser("add", help=tr("添加任务"))
    ta.add_argument("-f", "--file", help=tr("从 TOML 文件读取任务"))
    ta.add_argument("--title")
    ta.add_argument("--spec")
    ta.add_argument("--spec-file")
    ta.add_argument("--type", default="code_change")
    ta.add_argument("--difficulty", default="M", choices=["S", "M", "L"])
    ta.add_argument("--risk", default="normal", choices=["low", "normal", "high", "critical"])
    ta.add_argument("--scope", action="append", help=tr("可多次指定"))
    ta.add_argument("--verify", action="append", help=tr("验收命令，可多次指定"))
    ta.add_argument("--depends", action="append", help=tr("依赖的任务 id"))
    ta.add_argument("--max-runs", type=int)
    ta.add_argument("--review", default="auto", choices=["auto", "always", "never"],
                    help=tr("LLM 审查：auto = 按配置（默认只审 risk>=high 或没有验收命令的）"))
    ta.add_argument("--allow-duplicate", action="store_true", help=tr("允许和已有任务同名"))
    tsub.add_parser("list")
    ts = tsub.add_parser("show")
    ts.add_argument("id", help=tr("任务 id，或 latest"))
    trp = tsub.add_parser("retry", help=tr("把 blocked/failed 的任务重新排队"))
    trp.add_argument("id")
    trp.add_argument("--from-start", action="store_true", help=tr("从路由的第一级重新开始"))
    tc = tsub.add_parser("cancel", help=tr("取消排队中 / blocked / failed 的任务（task retry 可以恢复）"))
    tc.add_argument("ids", nargs="+")
    tac = tsub.add_parser("accept", help=tr("验收已通过、但审查一直不通过的任务：人工放行"))
    tac.add_argument("id")
    t.set_defaults(fn=cmd_task)

    r = sub.add_parser("run", help=tr("运行排队中的任务"))
    r.add_argument("ids", nargs="*")
    r.add_argument("--max-tasks", type=int)
    r.add_argument("--auto-merge", action="store_true", help=tr("验收和审查都通过后自动合并"))
    r.add_argument("--jobs", type=int, help=tr("同时跑几个任务（默认看 [parallel] max_jobs；1 = 一个接一个）"))
    r.set_defaults(fn=cmd_run)

    sub.add_parser("status", help=tr("状态总览")).set_defaults(fn=cmd_status)

    rp = sub.add_parser("report", help=tr("执行记录报表（类型 × 池、池 × 角色、任务明细）"))
    rp.add_argument("--since", help=tr("只看这之后的运行：7d 或 2026-10-01"))
    rp.add_argument("--type", help=tr("只看某种任务类型，例如 code_change"))
    rp.add_argument("--json", action="store_true", help=tr("输出 JSON"))
    rp.set_defaults(fn=cmd_report)

    cx = sub.add_parser("context", help=tr("查看某次运行实际注入的上下文（各部分 token、预算、检索、压缩）"))
    cx.add_argument("id", nargs="?", default="latest", help=tr("T-xxxx（该任务所有运行）、R-xxxx 或 latest"))
    cx.add_argument("--title", help=tr("预览：给出任务标题，不运行任何模型，只看会注入什么"))
    cx.add_argument("--spec", default="")
    cx.add_argument("--type", default="code_change")
    cx.add_argument("--difficulty", default="M", choices=["S", "M", "L"])
    cx.add_argument("--scope", action="append")
    cx.add_argument("--verify", action="append")
    cx.add_argument("--show", action="store_true", help=tr("预览时打印 CONTEXT.md 全文"))
    cx.set_defaults(fn=cmd_context)

    me = sub.add_parser("memory", help=tr("项目记忆（docs/project/）：init 创建 | show 查看 | commit 提交手工修改"))
    me.add_argument("action", choices=["init", "show", "commit"], nargs="?", default="show")
    me.set_defaults(fn=cmd_memory)

    m = sub.add_parser("merge", help=tr("合并已验收的任务"))
    m.add_argument("id")
    m.add_argument("--keep-worktree", action="store_true")
    m.set_defaults(fn=cmd_merge)

    ag = sub.add_parser("agents", help=tr("总控面板：每个 agent 的状态、用量、成功率；pause|resume 池名，swap 任务 池名"))
    ag.add_argument("--json", action="store_true", help=tr("机器可读（给网页或别的工具用）"))
    agsub = ag.add_subparsers(dest="agents_cmd")
    agp = agsub.add_parser("pause", help=tr("暂停一个 agent：不再派新任务（正在跑的不打断）"))
    agp.add_argument("pool")
    agp.add_argument("--minutes", type=float, default=None, help=tr("暂停多久；不填 = 直到 resume"))
    agsub.add_parser("resume", help=tr("恢复一个 agent")).add_argument("pool")
    ags = agsub.add_parser("swap", help=tr("把一个任务换给另一个 agent 先做，并重新排队"))
    ags.add_argument("task")
    ags.add_argument("pool")
    ag.set_defaults(fn=cmd_agents)

    se = sub.add_parser("sessions", help=tr("本机的 Claude Code / Codex 会话（含手动开的）：时间、项目、用量；只读"))
    se.add_argument("--days", type=float, default=7)
    se.add_argument("--limit", type=int, default=30)
    se.add_argument("--all", action="store_true", help=tr("也列出 orch 自己开的会话"))
    se.add_argument("--json", action="store_true")
    se.set_defaults(fn=cmd_sessions)

    b = sub.add_parser("breaker", help=tr("熔断器"))
    bsub = b.add_subparsers(dest="breaker_cmd", required=True)
    br = bsub.add_parser("reset")
    br.add_argument("pool", nargs="?")
    bo = bsub.add_parser("open", help=tr("手动暂停一个池（例如把任务从 Claude 切给 Codex）"))
    bo.add_argument("pool")
    bo.add_argument("--minutes", type=float, default=60)
    b.set_defaults(fn=cmd_breaker)

    pl = sub.add_parser("plan", help=tr("M4：让 Manager 把目标拆成任务。plan \"目标\" | show | approve ID | reject ID | list"))
    pl.add_argument("words", nargs="*")
    pl.add_argument("--context-file", help=tr("给 Manager 的补充说明（文本文件）"))
    pl.add_argument("--pool", help=tr("指定用哪个池做规划（默认按 [routing] 的 plan 规则）"))
    pl.set_defaults(fn=cmd_plan)

    dc = sub.add_parser("decide", help=tr("M5：两家出方案、互评、裁判写 ADR。decide \"问题\" | show | accept ID | reject ID | list"))
    dc.add_argument("words", nargs="*")
    dc.add_argument("--context-file", help=tr("补充说明（文本文件）"))
    dc.add_argument("--lang", help=tr("回答用的语言，例如 \"Simplified Chinese\"（默认看 [decide] language）"))
    dc.set_defaults(fn=cmd_decide)

    ky = sub.add_parser("key", help=tr("save your own API keys (encrypted on Windows): key set|list|remove PROVIDER"))
    ky.add_argument("action", choices=["set", "list", "remove"])
    ky.add_argument("provider", nargs="?", help="deepseek, openai, anthropic, qwen, gemini, doubao")
    ky.set_defaults(fn=cmd_key)

    cn = sub.add_parser("connect", help=tr("show which models you can use, or add one to this project: connect [PROVIDER]"))
    cn.add_argument("provider", nargs="?")
    cn.add_argument("--for", dest="purpose", choices=["coding", "review"], default="coding")
    cn.add_argument("--model", help=tr("model to use (coding: provider/model as opencode names it)"))
    cn.set_defaults(fn=cmd_connect)

    mc = sub.add_parser("mcp", help=tr("MCP 服务（stdio）：在 Claude / ChatGPT 等聊天应用里查看和派发 orch 任务"))
    mc.add_argument("--project", dest="projects", action="append", help=tr("要管理的项目目录，可多次指定（默认 -p 的项目）"))
    mc.set_defaults(fn=cmd_mcp)

    ro = sub.add_parser("route", help=tr("v0.3：按执行记录给路由建议（suggest），你同意后再改配置（apply S-n）"))
    ro.add_argument("action", choices=["suggest", "apply"])
    ro.add_argument("id", nargs="?")
    ro.add_argument("--min", type=int, help=tr("给建议所需的最少任务数（默认 5）"))
    ro.set_defaults(fn=cmd_route)

    us = sub.add_parser("usage", help=tr("v0.3：从保存的事件流重算历史运行的 token（含缓存命中、调用轮数）。usage rescan"))
    us.add_argument("action", choices=["rescan"])
    us.set_defaults(fn=cmd_usage)

    ap = sub.add_parser("api", help=tr("v0.3：检查直接 API 池（key、模型列表、一次极小的调用）。api check deepseek_api"))
    ap.add_argument("action", choices=["check"])
    ap.add_argument("pool", nargs="?", default="deepseek_api")
    ap.set_defaults(fn=cmd_api)

    hx = sub.add_parser("handoff", help=tr("V2-7：生成项目交接文件（Checkpoint），给新的 AI 会话读。handoff export"))
    hx.add_argument("action", choices=["export"])
    hx.add_argument("--out", help=tr("写到这个文件（默认 .agents/PROJECT_HANDOFF.md）"))
    hx.add_argument("--no-commit", action="store_true", help=tr("不提交到 integration 分支的 docs/project/CHECKPOINT.md"))
    hx.set_defaults(fn=cmd_handoff)

    h = sub.add_parser("handoff-test", help=tr("创建真实交接测试项目（Claude -> Codex）"))
    h.add_argument("dir", nargs="?", default="playground/handoff")
    h.set_defaults(fn=cmd_handoff_test)

    d = sub.add_parser("demo", help=tr("创建离线演示项目"))
    d.add_argument("dir", nargs="?", default="playground/demo")
    d.set_defaults(fn=cmd_demo)

    pr = sub.add_parser("probe", help=tr("M0：用真实 CLI 跑一个极小任务"))
    pr.add_argument("executor", choices=["claude_code", "codex", "opencode"])
    pr.add_argument("--model", default="")
    pr.add_argument("--dir", default="probe")
    pr.add_argument("--timeout", type=float, default=10, help=tr("分钟"))
    pr.set_defaults(fn=cmd_probe)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if sys.version_info < (3, 11):
        print(tr("需要 Python 3.11 或更新版本"), file=sys.stderr)
        return 2
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--lang" in argv[:-1]:  # before the parser is built: its help texts are translated too
        i18n.set_lang(argv[argv.index("--lang") + 1])
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except OrchError as e:
        print(tr("错误: {0}", e), file=sys.stderr)
        return 1
    finally:
        while _OPENED:
            _OPENED.pop().store.close()
