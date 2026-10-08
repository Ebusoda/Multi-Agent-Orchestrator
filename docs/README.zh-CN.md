# MAO：多模型编排器（中文说明）

一个跑在你自己电脑上的个人 AI 调度系统。它把 Claude Code、Codex（GPT）、DeepSeek、豆包、千问等模型当作可以互相替换的「执行者」：
按任务难度和成本分派任务，测试通过才算完成，失败时带着交接说明换一个模型接手。每次调用只给模型完成任务所需的最小上下文，
省 token、省钱。

只需要 Python 3.11 以上，不用安装任何第三方库。在 Windows 上开发和使用。

## 现在能用它做什么

- 在命令行里，或者在 Claude Desktop 这类聊天应用里（MCP），把编程任务交给你自己的模型。测试通过才算完成。
- 用你已有的账号：Claude Code、Codex 的订阅，以及 DeepSeek、GPT、Claude、千问、Gemini、豆包的 API key（`orch connect` 会列出哪些能用）。
- 每个任务先交给合适的最便宜的模型；失败、崩溃、被限流或卡住时换更强的模型，下一个模型读交接说明接着做。
- 把一个大目标拆成任务，你批准后再执行（`orch plan`）；或者让两家模型各出方案、互相点评，裁判写决策记录（`orch decide`）。
- 同时跑多个任务，每个都在自己的 git worktree 里，自动或手动合并。
- 看每个模型花了多少钱、一次通过的比例（`orch report`），以及每次调用到底发了什么（`orch context`）。
- 在一个面板上看所有 agent：正在做什么、今天和这周用了多少、订阅额度、最近成功率和最近一次出错；可以暂停、恢复某个 agent，或者把任务换给另一个（`orch agents`）。
- 看到你在这台电脑上自己开的 Claude Code、Codex 会话，和 orch 派的活放在一起：哪个项目、什么模型、上次什么时候用、现在是不是在用、用了多少 token（`orch sessions`）。只从会话记录里读时间、文件夹和 token 数，不打开对话内容和登录文件。
- 用一个自动生成的文件把整个项目交给新的聊天会话（`orch handoff export`）。
- 先用假模型离线把这些都试一遍（`orch demo`）。

## 它能做什么

- **把活交给能做好的最便宜的模型**：每种任务类型和难度都有一个按顺序尝试的「池」列表（池 = 命令行工具或 API + 模型 + 计费方式）。
  简单的编码任务先给 DeepSeek，难的给 Codex 或 Claude。
- **只信测试，不信模型自己说**：任务的验收命令全部通过（退出码为 0），并且合并后整个项目的基线测试还能通过，才算完成。
  只有客观失败才会升级到更贵的模型。
- **卡住了就提前换人**：一次运行没有改动任何文件，或者验收报错和上一次完全一样，就马上换下一个模型，不把剩下的次数浪费在同一个模型上。
- **出了问题能接着做**：每个任务都在自己的 git worktree 和分支里运行，每次运行后自动做 checkpoint 提交。
  崩溃、限流、按 Ctrl+C 都会触发熔断，下一个模型读取自动生成的 `HANDOFF.md` 接着做，不需要别家模型的聊天记录。
- **只给最小必要的上下文**：上下文管理器从项目记忆（`docs/project/`，随项目一起版本化）里挑出和这个任务相关的内容，
  控制在 token 预算以内。每次调用给了什么都有记录（`orch context ID`）。
- **花了多少一目了然**：`orch report` 按任务类型和池列出首次通过率、每个成功任务的成本、缓存命中和调用轮数。
  还有单任务预算、订阅额度的节奏闸门，以及需要你批准才生效的路由建议（`orch route suggest`）。
- **只在测试覆盖不到的地方审查**：高风险或没有测试的改动，由另一家公司的模型只读审查；
  审查通过直接 API 只发规格和改动（约 1 千 token，原来要 8.5 万）。

## 原理

```
命令行 / MCP ──▶ 编排器（确定性的状态机，控制决策不交给任何 LLM）
                 ├─ 上下文管理器    项目记忆 → 检索 → 预算 → .task/CONTEXT.md
                 ├─ 模型与预算      路由阶梯、熔断、额度保留、节奏闸门、单任务预算
                 ├─ 执行者          Claude Code · Codex · opencode（DeepSeek 等）· 直接 API
                 ├─ 工作区          每个任务一个 git worktree、checkpoint、squash 合并
                 ├─ 验收            验收命令和基线命令决定成败
                 └─ 记忆写入        最近变更、决策索引、待确认的经验 → docs/project/
```

状态分三处保存：控制状态在 SQLite（`.agents/state.db`，只有编排器写），工作状态在 git，
认知状态在文件里（`PROGRESS.md`、`HANDOFF.md`、项目记忆）。没有任何状态只存在某个模型的会话里，所以任何模型都能接手任何任务。

## 从下载到第一个任务

需要 Python 3.11 以上和 git，此外不用安装任何东西。在 Windows 上开发和使用，下面的命令用 PowerShell。
请在下载下来的文件夹里运行（或者先运行一次 `pip install -e .`，之后就可以直接输入 `orch`）。

**1. 下载，先跑离线演示**（假执行者，不需要账号，不花钱）：

```powershell
git clone https://github.com/Ebusoda/Multi-Agent-Orchestrator.git
cd Multi-Agent-Orchestrator
python -m orch demo playground/demo
python -m orch -p playground/demo run --auto-merge
python -m orch -p playground/demo status
```

**2. 连接你自己的模型**：`python -m orch connect` 会列出现在能用哪些。

- **订阅**：安装并登录你付费的命令行工具。Claude Code（`npm install -g @anthropic-ai/claude-code`，然后运行一次 `claude`）
  用你的 Claude 套餐；Codex（`npm install -g @openai/codex`，然后运行一次 `codex`）用你的 ChatGPT 套餐。
- **API key**（DeepSeek、GPT、Claude、千问、Gemini、豆包）：`python -m orch key set deepseek` 保存 key
  （输入时不显示；在 Windows 上会按你的账号加密），或者设置对应的环境变量（`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、
  `ANTHROPIC_API_KEY`、`DASHSCOPE_API_KEY`、`GEMINI_API_KEY`、`ARK_API_KEY`）。`python -m orch key list` 查看哪些已设置。
  用 API 模型写代码需要 opencode（`npm install -g opencode-ai`）。

**3. 设置你的项目**（任意至少有一次提交的 git 仓库）：

```powershell
python -m orch -p 你的仓库 init
python -m orch -p 你的仓库 connect deepseek              # 小任务和中等任务先交给 DeepSeek
python -m orch -p 你的仓库 connect openai --for review   # 有风险的改动由 GPT 通过 API 审查
python -m orch -p 你的仓库 api check openai_api          # 发一次极小的调用，检查 key
python -m orch -p 你的仓库 doctor
```

路由、预算等所有设置都在 `你的仓库\.agents\orch.toml` 里（带中文注释）。

**4. 跑第一个任务**：写上验收命令，它们全部返回 0 才算完成。

```powershell
python -m orch -p 你的仓库 task add --title "Add a --version flag" --difficulty S --verify "python -m unittest -q"
python -m orch -p 你的仓库 run --auto-merge
python -m orch -p 你的仓库 status
python -m orch -p 你的仓库 report
```

结果会合并到 `orch/integration` 分支，你自己的工作目录不会被改动。满意了再把这个分支合并进你的分支。

**语言**：orch 的提示有中文、英文、日文三种，默认跟随系统语言。用 `python -m orch lang zh`（或 `en`、`ja`）切换；只想这一次换，就加 `--lang en`。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `task add / list / show / retry / cancel / accept` | 管理任务 |
| `run [--jobs N] [--auto-merge]` | 运行排队的任务；改不同文件的任务可以并行 |
| `plan "目标"` / `plan approve P-0001` | 只读规划，拆成任务，你批准后才建 |
| `decide "问题"` | 两家各出方案、互相点评、裁判写决策记录 |
| `context ID` / `context --title ...` | 某次调用实际给了（或将会给）模型什么 |
| `memory init / show / commit` | 项目记忆 |
| `agents`、`agents pause / resume 池名`、`agents swap 任务 池名` | 总控面板：每个 agent 的状态和用量；暂停、恢复、换人 |
| `sessions [--days N] [--all]` | 本机的 Claude Code / Codex 会话，包括你手动开的（只读） |
| `report`、`route suggest / apply` | 各类任务在各个池上的成本和质量；路由建议 |
| `handoff export` | 生成项目交接文件，给新的聊天会话读 |
| `lang [zh/en/ja]` | 切换 orch 提示的语言 |
| `key set / list / remove 服务商`、`connect [服务商]` | 用你自己的模型账号 |
| `mcp --project 目录` | MCP 服务：在 Claude Desktop 等聊天应用里查看、派发、运行任务 |
| `api check 池名`、`usage rescan`、`status`、`merge`、`breaker` | 维护 |

## 安全

- 编排器从不改动你自己的工作目录，合并在单独的 integration worktree 里进行。
- agent 会在自己的 worktree 里执行任意命令，请只用在你自己的仓库上。
- 编排器不读取、不复制任何登录凭证，命令行工具各自用自己的登录。

## 更新记录

- **0.3.5**：`orch agents` 总控面板：每个 agent 正在做什么、今天和这周的用量、订阅额度、最近成功率和最近一次出错，
  可以暂停、恢复、换人。`orch sessions` 列出本机的 Claude Code、Codex 会话，包括你自己开的（只读时间、文件夹和 token 数）。
  orch 的提示支持中文、英文、日文（`orch lang`、`--lang`，默认跟随系统语言）。
- **0.3.4**：提前升级：一次运行什么都没改，或者验收报错和上一次完全一样，就马上换下一个模型（`[escalation] early_escalation`，默认打开）。
  README 写清楚了现在能用它做什么。
- **0.3.2**：`orch key` 和 `orch connect`：在命令行里用你自己的 DeepSeek、GPT、Claude、千问、Gemini、豆包账号
  （Windows 上 key 加密保存）；写清楚从下载到第一个任务的步骤。
- **0.3.1**：MCP 服务（`orch mcp`），可以在 Claude Desktop 等聊天应用里查看、添加、运行和跟进任务；
  合并冲突交给 agent 解决，没有冲突标记、验收通过才合并。
- **0.3.0**：首次公开发布：路由阶梯与升级、git worktree 隔离与交接、上下文管理器和版本化的项目记忆、
  每次调用的上下文清单、成本报表、只在测试薄弱处审查、审查走直接 API、单任务预算、节奏闸门、路由建议。

## 许可

[PolyForm Noncommercial 1.0.0](../LICENSE)：可以免费使用、修改、分享，但不能用于商业目的。
