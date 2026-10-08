# MAO: Multi-Agent Orchestrator

A personal AI runtime that runs on your own PC. It treats Claude Code, Codex (GPT), DeepSeek, Doubao, Qwen and
other models as interchangeable workers: it routes each task by difficulty and cost, accepts work only when its
tests pass, and hands failed work to another model with a handoff note. Each call gets only the minimum context it
needs, which keeps token use and cost down.

Python 3.11+, standard library only. Built and used on Windows. A full guide in Chinese is in
[docs/README.zh-CN.md](docs/README.zh-CN.md).

## What you can do with it today

- Give a coding task to your own models from the command line, or from a chat app such as Claude Desktop (MCP).
  It counts as done only when your tests pass.
- Use the accounts you already have: Claude Code and Codex subscriptions, and API keys for DeepSeek, GPT, Claude,
  Qwen, Gemini and Doubao (`orch connect` shows what works).
- Start each task on the cheapest suitable model and move it to a stronger one when it fails, crashes, hits a
  rate limit or gets stuck; the next model continues from a handoff note.
- Turn a larger goal into tasks you approve (`orch plan`), or let two vendors propose and critique a design and a
  judge write the decision record (`orch decide`).
- Run several tasks at once, each in its own git worktree, and merge them automatically or by hand.
- See what every model cost and how often it passed first time (`orch report`), and exactly what each call was sent
  (`orch context`).
- See every agent on one panel: what it is working on, what it used today and this week, its subscription
  windows, recent success rate and last error; pause or resume an agent, or give a task to another one
  (`orch agents`).
- See the Claude Code and Codex sessions you open yourself on this computer, next to orch's own: project, model,
  last use, whether one is in use now and its tokens (`orch sessions`). Only times, folders and token counts are
  read from the CLIs' session logs; conversation content and sign-in files are never opened.
- Hand the whole project to a new chat session with one generated file (`orch handoff export`).
- Try all of it offline with fake models first (`orch demo`).

## What it does

- **Routes work to the cheapest model that can do it.** Each task type and difficulty has an ordered list of
  pools (a pool is a CLI or API plus a model plus a billing source). Small coding tasks start on DeepSeek; harder
  ones on Codex or Claude.
- **Trusts tests, not the model.** A task is done when its acceptance commands exit with 0, and the project's
  baseline tests still pass after the merge. Escalation happens only on objective failures.
- **Leaves stuck models early.** When a run changes no files, or acceptance fails with exactly the same error as
  the previous run, the task moves to the next model at once instead of using up its remaining attempts.
- **Survives failures.** Every task runs in its own git worktree and branch, with a checkpoint commit after each
  run. Crashes, rate limits and Ctrl+C trip a circuit breaker; the next model takes over from a deterministic
  `HANDOFF.md`, never from another vendor's chat transcript.
- **Sends the minimum necessary context.** The Context Manager builds a per-task package from versioned project
  memory (`docs/project/`): goals and constraints always, other entries only if they relate to the task's files,
  all within a token budget. Every call records what it was given (`orch context ID`).
- **Keeps spending visible.** `orch report` shows, per task type and pool, the first-pass rate and the cost per
  successful task, cache hits and model calls. Per-task budgets, a pacing gate for subscription windows and
  routing suggestions you approve (`orch route suggest`) keep it in check.
- **Reviews only where tests are weak.** High-risk or untested work gets a read-only review from a different
  vendor, sent through a direct API with just the spec and the diff (about 1k tokens instead of 85k).

## How it works

```
CLI / MCP ──▶ Orchestrator (deterministic state machine; no LLM makes control decisions)
               ├─ Context Manager    project memory → retrieval → budget → .task/CONTEXT.md
               ├─ Model/Budget       routing ladder, breakers, quota reserve, pacing gate, budgets
               ├─ Executors          Claude Code · Codex · opencode (DeepSeek…) · direct API
               ├─ Workspace          one git worktree per task, checkpoints, squash merge
               ├─ Verify             acceptance + baseline commands decide success
               └─ Memory Writer      recent changes, ADR index, candidate lessons → docs/project/
```

State is kept in three places: control state in SQLite (`.agents/state.db`, written only by the
orchestrator), work state in git, and cognitive state in files (`PROGRESS.md`, `HANDOFF.md`, project memory).
Nothing lives only inside one model's session, so any model can pick up any task.

## Getting started

You need Python 3.11 or newer and git. MAO itself installs nothing else. It is developed and used on Windows;
commands below are for PowerShell. Run them from the folder you cloned into (or `pip install -e .` once,
then type `orch` instead of `python -m orch`).

**1. Download and try the offline demo** (fake executors, no account, no cost):

```powershell
git clone https://github.com/Ebusoda/Multi-Agent-Orchestrator.git
cd Multi-Agent-Orchestrator
python -m orch demo playground/demo
python -m orch -p playground/demo run --auto-merge
python -m orch -p playground/demo status
```

**2. Connect your own models.** `python -m orch connect` shows what you can use right now.

- *Subscriptions*: install and sign in to the CLIs you pay for. Claude Code
  (`npm install -g @anthropic-ai/claude-code`, then run `claude` once) uses your Claude plan; Codex
  (`npm install -g @openai/codex`, then run `codex` once) uses your ChatGPT plan.
- *API keys* (DeepSeek, GPT, Claude, Qwen, Gemini, Doubao): save a key with
  `python -m orch key set deepseek` (input is hidden; on Windows the key is encrypted for your account), or set the
  provider's environment variable (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DASHSCOPE_API_KEY`,
  `GEMINI_API_KEY`, `ARK_API_KEY`). `python -m orch key list` shows which are set. Coding with an API model goes
  through opencode (`npm install -g opencode-ai`).

**3. Set up your project** (any git repository with at least one commit):

```powershell
python -m orch -p path\to\repo init
python -m orch -p path\to\repo connect deepseek              # small and medium tasks start on DeepSeek
python -m orch -p path\to\repo connect openai --for review   # GPT reviews risky work through the API
python -m orch -p path\to\repo api check openai_api          # one tiny call to check the key
python -m orch -p path\to\repo doctor
```

Routes, budgets and every other setting live in `path\to\repo\.agents\orch.toml` (commented).

**4. Run your first task.** Give acceptance commands: a task is done only when they exit with code 0.

```powershell
python -m orch -p path\to\repo task add --title "Add a --version flag" --difficulty S --verify "python -m unittest -q"
python -m orch -p path\to\repo run --auto-merge
python -m orch -p path\to\repo status
python -m orch -p path\to\repo report
```

The result is merged into the `orch/integration` branch; your own checkout is never touched. Merge that branch
into yours when you are happy with it.

**Language.** orch speaks Chinese, English or Japanese. It follows your system language; change it with
`python -m orch lang en` (or `zh`, `ja`), or for one command with `--lang en`.

## Main commands

| Command | Purpose |
| --- | --- |
| `task add / list / show / retry / cancel / accept` | manage tasks |
| `run [--jobs N] [--auto-merge]` | run queued tasks, in parallel where scopes do not overlap |
| `plan "goal"` / `plan approve P-0001` | read-only planning into tasks, approved by you |
| `decide "question"` | two proposals from different vendors, critiques, a judge, an ADR |
| `context ID` / `context --title ...` | what a call was (or would be) given |
| `memory init / show / commit` | versioned project memory |
| `agents`, `agents pause / resume POOL`, `agents swap TASK POOL` | control panel: every agent's state and use; pause, resume, reassign |
| `sessions [--days N] [--all]` | Claude Code / Codex sessions on this computer, hand-opened ones included (read-only) |
| `report`, `route suggest / apply` | cost and quality per task type and pool; routing suggestions |
| `handoff export` | a project checkpoint for a new chat session |
| `key set / list / remove PROVIDER`, `connect [PROVIDER]` | your own model accounts |
| `lang [zh/en/ja]` | language of orch's messages |
| `mcp --project DIR` | MCP server for chat apps |
| `api check POOL`, `usage rescan`, `status`, `merge`, `breaker` | maintenance |

## Use it from a chat app (MCP)

`python -m orch mcp --project <repo> [--project <repo2>]` is a Model Context Protocol server over stdio. Add it to
an MCP client such as Claude Desktop (`claude_desktop_config.json`):

```json
{"mcpServers": {"orch": {"command": "python", "args": ["-m", "orch", "mcp", "--project", "F:/work/my-repo"],
                         "cwd": "F:/MAO"}}}
```

Tools: `orch_list_projects`, `orch_list_tasks`, `orch_task`, `orch_add_task`, `orch_run` (background),
`orch_report`, `orch_context`, `orch_merge`, and the control panel: `orch_agents` (all projects at once),
`orch_pause`, `orch_resume`, `orch_swap`, `orch_retry`, `orch_cancel`, `orch_sessions`. The chat model asks; orch still decides routing, acceptance and merging.

## Safety

- The orchestrator never touches your own checkout; merges happen in a separate integration worktree.
- Agents run arbitrary commands inside their worktree. Use it on your own repositories.
- No credentials are read or copied by the orchestrator; CLIs use their own sign-in.

## Changelog

- **0.3.5**: `orch agents`, one panel for every agent: what it is doing, usage today and this week, subscription
  quota, recent success rate and last error, with pause, resume and swap. `orch sessions` lists the Claude Code
  and Codex sessions on this computer, including ones you opened yourself (read-only: times, folder and tokens only).
  orch speaks Chinese, English and Japanese (`orch lang`, `--lang`, follows the system language).
- **0.3.4**: early escalation: a run that changes nothing, or fails acceptance exactly like the previous run, moves
  the task to the next model straight away (`[escalation] early_escalation`, on by default). README lists what you
  can do today.
- **0.3.2**: `orch key` and `orch connect`: use your own DeepSeek, GPT, Claude, Qwen, Gemini and Doubao
  accounts from the command line (keys encrypted on Windows); step-by-step getting started.
- **0.3.1**: MCP server (`orch mcp`) to list, add, run and follow tasks from chat apps such as Claude Desktop;
  merge conflicts are handed to an agent and merged only when no conflict markers remain and acceptance passes.
- **0.3.0**: first public release: routing ladder and escalation, git worktree isolation and handoffs, Context
  Manager with versioned project memory, per-call context manifests, cost report, review only where tests are
  weak, direct API for reviews, per-task budgets, pacing gate, routing suggestions.

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free to use, modify and share for any noncommercial purpose.
Commercial use is not permitted.
