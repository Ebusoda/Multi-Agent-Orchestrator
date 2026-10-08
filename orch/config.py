"""Project configuration: <project>/.agents/orch.toml merged over built-in defaults."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

AGENTS_DIR = ".agents"
CONFIG_NAME = "orch.toml"

# Written by `orch init`. Comments are in Chinese for the owner; keys stay English.
DEFAULT_CONFIG_TOML = """\
# MAO Orchestrator 配置（v0.1-pre）
# 修改后无需重启，下次 `orch run` 生效。

[project]
base_branch = ""                       # 空 = 使用 init 时的当前分支
integration_branch = "orch/integration"
worktrees_dir = ""                     # 空 = <项目目录>.worktrees（放在项目外面）

[escalation]
verify_failures = 2                    # 同一执行者验收连续失败几次就升级到下一个池
early_escalation = true                # 不等次数用完就升级：这次运行没改任何文件，或者验收报错和上一次完全一样
max_runs_per_task = 4                  # 单个任务最多让 agent 实际工作几次（崩溃、限流不计入）
run_timeout_minutes = 45               # 单次运行超时
breaker_threshold = 2                  # 同一池连续几次崩溃类失败就熔断（限流 / 认证失败会立即熔断）
breaker_cooldown_minutes = 30          # 熔断后多久再试（限流时优先用 resetsAt）
verify_timeout_minutes = 15

[parallel]
max_jobs = 2                           # orch run 同时跑几个任务（--jobs 可临时覆盖）；1 = 一个接一个
# 每个池同时最多几个运行，由池里的 max_concurrent 决定（默认 1）。路由首选的池忙时，任务会等它，
# 不会因为"忙"就跳到更贵的池。改同一批文件（scope 有重叠，或没写 scope）的任务不会同时跑。

[review]
# M5：验收通过后、合并之前，由另一家公司的模型只读审查改动（作者是 DeepSeek 就让 Codex 审，作者是 Codex 就让 Claude 审）。
enabled = true
pools = ["codex_sub", "claude_sub"]    # 审查者候选，按顺序挑第一个和作者不同公司、且当前可用的
max_fix_rounds = 1                     # 审查者退回几次后还不通过，就停下来交给你（blocked）
# V2-3：先靠验收命令这类确定性检查，LLM 审查只用在它们最弱的地方。默认只审：
require_risk = ["high", "critical"]    # 这些风险等级的任务
when_no_verify = true                  # 以及没有验收命令的任务
skip_risk = ["low"]                    # 这些风险等级一律不审（优先于上面）
# 单个任务可以用 task add --review always / never 覆盖。

[pacing]
# 节奏闸门（v0.3）：按订阅窗口（Claude 的 5 小时 / 7 天）到目前为止的消耗速度，预测到重置时会用掉多少。
# 预测 <= soft：所有任务都能用这个池；<= hard：只放行 L 级或 risk 为 high / critical 的任务；更高：只放行 critical。
# 被挡住的任务会走路由里的下一个池；没有下一个就停下交给你。窗口刚开始时速度不准，已过时间按至少 10% 算。
enabled = true
soft = 0.8
hard = 1.0
min_elapsed = 0.1

[budget]
# V2-6：单个任务（含它的所有执行和审查运行）最多花多少，超了就停下（blocked）交给你。0 = 不限。
max_usd_per_task = 0.50                # 只算按量付费的 API 池（opencode 池，或池里写 billing = "api"）的真实美元
max_tokens_per_task = 0                # 所有池的输入 + 输出 token（订阅池没有美元数，用这个限）

[merge]
# V2-5：合并进 integration 分支后，除了该任务自己的验收命令，再跑这些项目级命令（例如整套测试）。
# 任何一条失败就撤销这次合并。空 = 只跑任务自己的验收。
baseline = []                          # 例如 ["python -m unittest -q"]
resolve_conflicts = true               # 合并冲突时，把 integration 合进任务分支，交给 agent 解决；没有冲突标记且验收通过才合并

[git]
# orch 自己做的提交（checkpoint、合并、项目记忆、决策记录）用谁的名字。
# 空 = 用这个项目的 git 配置（git config user.name / user.email）；都没有时才用 orch <orch@localhost>。
name = ""                              # 例如 "Your Name"
email = ""                             # 例如 "you@example.com"

[approval]
manual_merge_risk = ["high", "critical"]   # 这些风险等级的任务即使 run --auto-merge 也不自动合并，等你 orch merge

[decide]
# M5：orch decide "问题"。两家各出方案 -> 互相点评 -> 裁判写决策记录（ADR），最多 5 次调用。
pools = ["claude_sub", "codex_sub"]    # 出方案的池（取前两个不同公司的）
judge = ["claude_sub", "codex_sub"]    # 裁判，按顺序取第一个可用的
docs_dir = "docs/decisions"            # ADR 提交到 integration 分支的这个目录
language = ""                          # 空 = 跟问题用同一种语言；也可以写 "Simplified Chinese"

[context]
# V2-1 Context Manager：每次调用只给模型"完成这个任务所需的最小上下文"，不发历史。
# 预算只算 orch 注入的部分（PROMPT / TASK / HANDOFF / REVIEW / VERIFY / CONTEXT.md），按任务档位取上限：
# S -> small，M -> normal，L -> large；接班（takeover）至少 normal。超了就把低分的记忆条目改成指针，再不够就丢掉。
enabled = true                         # false = 不读 docs/project/ 的项目记忆（仍会测量和记录注入量）
budgets = { small = 1500, normal = 3000, large = 6000, deep = 12000 }
# V2-7 会话轮换：验收失败 / 审查退回后，本来会续用原会话修改。会话越长每次调用越贵，所以满足任一条就换新会话 + 交接：
resume_max_tokens = 100000             # 上一次调用的输入 token 达到这个数（池里也可以单独写 resume_max_tokens）
max_resumes_per_session = 2            # 同一个会话已经续用了这么多次
# class_by_type = { research = "large" }   # 按任务类型指定档位（可选）

[routing]
# 任务类型.难度 = 按顺序尝试的池；排在前面的先用，失败后沿列表升级。
code_change.S = ["deepseek", "codex_sub"]
code_change.M = ["deepseek", "codex_sub", "claude_sub"]
code_change.L = ["codex_sub", "claude_sub"]
default = ["codex_sub", "claude_sub"]
plan = ["claude_sub", "codex_sub"]     # orch plan：拆分任务用（只读运行）
# 角色也是普通路由条目（V2-4）。写在这里就以这里为准；没写时沿用 [review] pools、[decide] pools / judge。
# deepseek_api 是直接 API（v0.3）：只收规格和 diff，不带执行壳的开销；没设 DEEPSEEK_API_KEY 时自动跳过。
# 作者是 DeepSeek 时它不能审（同一家公司），会轮到 codex_sub。
review = ["deepseek_api", "codex_sub", "claude_sub"]   # 审查者候选：按顺序挑第一个和作者不同公司、且可用的
decide = ["claude_sub", "codex_sub"]   # orch decide 出方案的池（取前两个不同公司的；要读代码，不能用直接 API）
judge = ["deepseek_api", "claude_sub", "codex_sub"]    # 裁判只读两份方案：第三家公司、最便宜

# ---- 额度池：执行壳 + 模型 ----
[pools.claude_sub]
executor = "claude_code"
model = ""                             # 空 = 使用 Claude Code 默认模型（可填 sonnet / opus）
reserve = 0.10                         # 5 小时或 7 天窗口用到 90% 后，只接 risk=critical 的任务
resume_max_tokens = 100000             # 会话轮换阈值：约为模型上下文窗口的一半

[pools.codex_sub]
executor = "codex"
model = ""
reserve = 0.10                         # Codex 目前不在事件里报告用量，这一项暂时不起作用
resume_max_tokens = 200000

[pools.deepseek]
executor = "opencode"
model = "deepseek/deepseek-flash"      # 以 `opencode models` 的输出为准
max_concurrent = 2                     # 按量付费的 API 可以同时跑多个；订阅池保持默认 1，避免一口气烧掉额度
resume_max_tokens = 64000

[pools.deepseek_api]
# v0.3 直接 API（OpenAI 兼容接口，只用标准库 urllib）。只用于审查、裁判这类不需要工具的步骤，不会接编码任务。
executor = "api"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"       # key 只从这个环境变量读，不写进任何文件
model = "deepseek-flash"               # 与 opencode 的 deepseek/deepseek-flash 相同；以 orch api check 列出的为准
# 每百万 token 的美元价格；0 = 不算美元，只记 token。下面是 deepseek-flash 的官方峰时价（非峰时减半），
# 2026-10-08 查自 https://api-docs.deepseek.com/quick_start/pricing/ ，按峰时算偏保守。
price_in_per_m = 0.30                  # 输入，未命中缓存
price_cache_hit_per_m = 0.006          # 输入，命中缓存
price_out_per_m = 1.20
max_concurrent = 2

# ---- 执行壳（CLI）参数 ----
# 提示词不走命令行参数：Orchestrator 写入 .task/PROMPT.md，再让 CLI 去读它，
# 这样可以避开 Windows 上 .cmd 包装器的引号转义问题。
[executors.claude_code]
command = "claude"
# --strict-mcp-config：不加载你账号里的连接器 / MCP（实测会多出大量工具定义，白白消耗额度）
args = ["--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits", "--strict-mcp-config"]
# 只给 agent 这些内置工具（不含定时任务、联网搜索、远程触发等）
tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "PowerShell"]
# Windows 上 Claude Code 跑命令用 Git Bash（Bash）或 PowerShell，两个都放行。
allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "PowerShell"]
prompt_via_stdin = true                # 提示词通过 stdin 直接给，少一轮读文件（实测 T-0013 正常）

[executors.codex]
command = "codex"
# windows.sandbox=unelevated：Codex 在 Windows 上默认用单独的低权限用户跑命令，读不到装在你用户目录里的
# Python / Node。unelevated 改为用你自己的账号 + 受限权限运行，仍然禁止写工作目录以外的地方，但隔离比默认弱。
# 想要更强的隔离：删掉这两项，并把 Python 装成"所有用户"（装在 C:/Program Files 下）。在 macOS / Linux 上这项无效。
args = ["--json", "--sandbox", "workspace-write", "-c", "windows.sandbox=unelevated"]
prompt_via_stdin = true                # 提示词作为 <stdin> 块直接给 Codex（官方支持）。实测 T-0012：15 轮 -> 5 轮

[executors.opencode]
command = "opencode"
args = ["--format", "json"]
# 如果 probe 显示 opencode 在等待权限确认，可加 "--auto"（自动批准未被显式禁止的操作，有风险）。

# 以上参数已在 Windows 上用真实任务验证过（orch probe）：claude 2.1.292 / codex-cli 0.160.1 / opencode 1.18.35。
"""

DEFAULTS: dict[str, Any] = tomllib.loads(DEFAULT_CONFIG_TOML)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, project: Path, data: dict[str, Any]):
        self.project = project
        self.data = data

    @classmethod
    def load(cls, project: Path) -> "Config":
        path = project / AGENTS_DIR / CONFIG_NAME
        user: dict[str, Any] = {}
        if path.exists():
            with path.open("rb") as f:
                user = tomllib.load(f)
        # routing / pools are replaced wholesale when the user defines them,
        # otherwise a default pool the user deleted would silently come back.
        data = _deep_merge({k: v for k, v in DEFAULTS.items() if k not in ("routing", "pools")}, user)
        data.setdefault("routing", copy.deepcopy(DEFAULTS["routing"]))
        data.setdefault("pools", copy.deepcopy(DEFAULTS["pools"]))
        data["executors"] = _deep_merge(DEFAULTS["executors"], user.get("executors", {}))
        from . import workspace  # orch's own commits follow [git]

        g = data.get("git", {})
        workspace.set_identity(str(g.get("name") or ""), str(g.get("email") or ""))
        return cls(project, data)

    # -- paths -------------------------------------------------------------
    @property
    def agents_dir(self) -> Path:
        return self.project / AGENTS_DIR

    @property
    def worktrees_dir(self) -> Path:
        custom = self.data["project"].get("worktrees_dir") or ""
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else (self.project / p).resolve()
        return self.project.parent / f"{self.project.name}.worktrees"

    # -- sections ----------------------------------------------------------
    @property
    def esc(self) -> dict[str, Any]:
        return self.data["escalation"]

    @property
    def pools(self) -> dict[str, dict[str, Any]]:
        return self.data["pools"]

    def executor_cfg(self, name: str) -> dict[str, Any]:
        return self.data["executors"].get(name, {})

    def role_pools(self, role: str, legacy_section: str, legacy_key: str, difficulty: str = "M") -> list[str]:
        """Pools for a role (review / decide / judge). [routing] wins (V2-4); the old
        [review] pools / [decide] pools / judge settings still work when routing has no entry."""
        if role in self.data["routing"]:
            return self.route_for(role, difficulty)
        legacy = self.data.get(legacy_section, {}).get(legacy_key) or []
        return [legacy] if isinstance(legacy, str) else list(legacy)

    def route_for(self, task_type: str, difficulty: str) -> list[str]:
        routing = self.data["routing"]
        by_type = routing.get(task_type)
        if isinstance(by_type, dict):
            if difficulty in by_type:
                return list(by_type[difficulty])
            if "default" in by_type:
                return list(by_type["default"])
        elif isinstance(by_type, list):
            return list(by_type)
        return list(routing.get("default", []))
