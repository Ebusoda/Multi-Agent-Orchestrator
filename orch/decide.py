"""M5b: `orch decide` - a short structured debate that ends in an Architecture Decision Record.

  1. two proposers from different vendors answer the question independently (read-only)
  2. each critiques the other's proposal (resuming its own session when the CLI can)
  3. a judge weighs proposals + critiques and writes the ADR (status: proposed)

At most 5 agent calls. Critics and the judge see "Proposal A / B" without vendor names, so the
judge cannot simply prefer its own company's answer. The orchestrator writes the ADR header
(status, date, who proposed what) and commits the file to the integration branch, where later
tasks and plans can read it. The human accepts or rejects it with `orch decide accept|reject`.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import workspace as ws
from .adapters import Adapter, RunContext
from .models import INFRA_FAILURES, RunResult, RunStatus, Task
from .plan import PlanError, _finish_run

DECIDE_INSTRUCTION = (
    "Read the file .task/DECIDE_PROMPT.md in the current directory and do what it asks. "
    "Do not modify any file."
)
MAX_PROPOSAL_CHARS = 12_000


class DecideError(RuntimeError):
    pass


@dataclass
class Proposer:
    label: str        # "A" / "B"
    pool: str
    vendor: str
    adapter: Adapter
    session: str | None
    text: str


# -- prompts --------------------------------------------------------------------------

def _lang(language: str) -> str:
    return (f"Write your answer in {language}." if language.strip()
            else "Write your answer in the same language as the question.")


def proposal_prompt(question: str, context: str, language: str) -> str:
    extra = f"\n## Extra context from the user\n\n{context.strip()}\n" if context.strip() else ""
    return f"""<!-- orch:decide step=proposal -->
# Design question from the MAO orchestrator

You are one of two **independent architects**. Another architect answers the same question
separately; a judge will compare both answers. You are in **read-only mode**: do NOT create, edit
or delete files. Read whatever you need in the current directory (the project's integration
branch) so your answer fits the real code.

## Question

{question.strip()}
{extra}
## Your answer

Your final message is your proposal, in Markdown, under 700 words, with these sections:

- `## Recommendation` - what to do, in one or two sentences
- `## Approach` - how, concretely (files, interfaces, data, steps)
- `## Alternatives rejected` - and why
- `## Risks and trade-offs`
- `## Effort` - rough size (S / M / L) and what it touches

Be concrete and honest about uncertainty. {_lang(language)}
"""


def critique_prompt(question: str, own: Proposer, other: Proposer, language: str, resumed: bool) -> str:
    own_text = "" if resumed else (
        f"\n## Your own proposal (Proposal {own.label})\n\n{own.text[:MAX_PROPOSAL_CHARS]}\n"
    )
    return f"""<!-- orch:decide step=critique -->
# Critique round

The question was:

> {question.strip()}
{own_text}
## The other architect's proposal (Proposal {other.label})

{other.text[:MAX_PROPOSAL_CHARS]}

## Your task

Critique Proposal {other.label}. Read-only: do not modify files; check its claims against the code
where it matters. Your final message, in Markdown, under 400 words:

- `## Strongest points` - what it gets right that your proposal missed or did worse
- `## Weaknesses and risks` - concrete problems, wrong assumptions, missing cases
- `## Would you change your recommendation?` - yes/no and why

Be fair: the judge rewards accurate critique, not winning. {_lang(language)}
"""


def judge_prompt(question: str, proposers: list[Proposer], critiques: dict[str, str], language: str) -> str:
    parts = []
    for p in proposers:
        parts.append(f"## Proposal {p.label}\n\n{p.text[:MAX_PROPOSAL_CHARS]}\n")
        if p.label in critiques:
            parts.append(f"## Critique of Proposal {p.label} (by the other architect)\n\n"
                         f"{critiques[p.label][:MAX_PROPOSAL_CHARS]}\n")
    body = "\n".join(parts)
    options = "### Option A, ### Option B" if len(proposers) > 1 else "### Option A (and any alternative it rejected)"
    return f"""<!-- orch:decide step=judge -->
# Judge: write the decision record

You are the **judge**. Below are the answers of independent architects to a design question,
and their critiques of each other. Read-only: do not modify files, but do check disputed
claims against the code. You may pick one proposal, combine them, or recommend something
better; say which and why. A human makes the final decision from your record.

## Question

{question.strip()}

{body}
## Output

Your final message is the decision record itself, in Markdown, nothing before it. It must
start with a `# ` title line naming the decision (short, no "ADR" prefix), followed by exactly
these sections:

- `## Context` - the problem and the constraints that matter
- `## Options considered` - {options}: pros and cons of each
- `## Recommendation` - what to do and why; mention which proposal(s) it comes from
- `## Consequences` - what becomes easier, what becomes harder, follow-up work
- `## What would change this decision`
- `## Open questions for the human`

Do not add a status or date line; the orchestrator adds them. {_lang(language)}
"""


# -- parsing ----------------------------------------------------------------------------

_OUTER_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.S)


def extract_adr(text: str) -> tuple[str, str]:
    """(title, body) from the judge's final message. body starts at the first `# ` line."""
    text = (text or "").strip()
    m = _OUTER_FENCE.match(text)
    if m:
        text = m.group(1).strip()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# ") and line[2:].strip():
            title = line[2:].strip().strip("#").strip()
            title = re.sub(r"^(ADR|Decision)\s*[:\-]\s*", "", title, flags=re.I) or title
            body = "\n".join(lines[i + 1:]).strip()
            if "## " not in body:
                break
            return title, body
    raise PlanError("the judge's answer has no '# Title' line followed by '## ' sections")


def slugify(title: str, limit: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:limit].rstrip("-") or "decision"


def file_slug(title: str, question: str) -> str:
    """ASCII titles name the file; otherwise use the question's first sentence (ADR titles may be Chinese)."""
    if title.isascii():
        return slugify(title)
    first = re.split(r"[?.!？。！]", question, maxsplit=1)[0]
    slug = slugify(first)
    return slug if slug != "decision" else slugify(title)


def header(did: str, title: str, question: str, proposers: list[Proposer], judge: str,
           status: str = "proposed") -> str:
    who = "; ".join(f"{p.label} = {p.pool} ({p.vendor})" for p in proposers) or "-"
    q = " ".join(question.split())
    return (
        f"# {title}\n\n"
        f"- Status: {status}\n"
        f"- Date: {time.strftime('%Y-%m-%d')}\n"
        f"- Decision id: {did}\n"
        f"- Question: {q}\n"
        f"- Proposals: {who}; judge: {judge}\n"
    )


def fallback_body(proposers: list[Proposer], critiques: dict[str, str]) -> str:
    parts = ["## Context", "", "No judge was available, so the orchestrator collected the proposals "
             "unchanged. Read them and decide.", ""]
    for p in proposers:
        parts += [f"## Proposal {p.label}", "", p.text.strip(), ""]
        if p.label in critiques:
            parts += [f"### Critique of Proposal {p.label}", "", critiques[p.label].strip(), ""]
    return "\n".join(parts).strip()


# -- running ------------------------------------------------------------------------------

def _call(orch, did: str, pseudo: Task, wt: Path, out: Path, pool: str, adapter: Adapter,
          prompt: str, step: str, resume: str | None = None) -> RunResult | None:
    """One read-only agent call. None = no usable answer (the reason is already reported)."""
    (wt / ".task").mkdir(exist_ok=True)
    (wt / ".task" / "DECIDE_PROMPT.md").write_text(prompt, encoding="utf-8")
    (out / f"{step}.prompt.md").write_text(prompt, encoding="utf-8")
    run = orch.store.create_run(did, pool, adapter.executor, resumed=bool(resume), kind="decide")
    events = out / "runs" / f"{run.id}.{step}.jsonl"
    run.events_path = str(events)
    orch.store.save_run(run)

    def on_start(pid: int, run=run) -> None:
        run.pid = pid
        orch.store.save_run(run)

    orch.say(f"[{did}] {run.id} -> {pool} ({adapter.vendor}) {step}" + ("（续用自己的会话）" if resume else "") + "...")
    ctx = RunContext(task=pseudo, worktree=wt, events_path=events,
                     timeout_s=orch._minutes("run_timeout_minutes", 45), model=adapter.model,
                     readonly=True, instruction=DECIDE_INSTRUCTION, resume_session=resume,
                     on_start=on_start)
    result = adapter.run(ctx)
    _finish_run(orch.store, run, result)
    if ws.dirty(wt):
        ws.discard_changes(wt)
    if result.status == RunStatus.INTERRUPTED:
        orch.store.update_decision(did, status="failed", error="interrupted by user")
        raise KeyboardInterrupt
    if result.status in INFRA_FAILURES or result.status == RunStatus.TIMEOUT:
        orch.breakers.record_failure(pool, result.status, result.resets_at)
        orch.say(f"[{did}] {pool} 失败（{result.status.value}）")
        return None
    orch.breakers.record_success(pool)
    if result.status != RunStatus.COMPLETED or not (result.summary or "").strip():
        orch.say(f"[{did}] {pool} 没有给出可用的回答（{result.status.value}）")
        return None
    return result


def run_decide(orch, question: str, context: str = "", language: str | None = None) -> str:
    """Runs the debate; returns the decision id. Raises DecideError if nobody could answer."""
    cfg = orch.cfg
    dcfg = cfg.data.get("decide", {})
    lang = dcfg.get("language", "") if language is None else language
    for row in orch.store.list_decisions():  # left "running" by a killed orchestrator (we hold the lock)
        if row["status"] == "running":
            orch.store.update_decision(row["id"], status="failed", error="orchestrator stopped during the debate")
    did = orch.store.new_decision(question)
    pseudo = Task(id=did, title="decide", type="decide", difficulty="L")
    wt = ws.ensure_detached_worktree(orch.project, cfg.worktrees_dir / "_decide", orch.integration_branch)
    out = cfg.agents_dir / "decisions" / did
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "QUESTION.md").write_text(question.strip() + ("\n\n" + context.strip() if context.strip() else "") + "\n",
                                     encoding="utf-8")

    # 1. proposals: at most two, from different vendors
    proposers: list[Proposer] = []
    candidates = orch.cfg.role_pools("decide", "decide", "pools")
    start = 0
    while len(proposers) < 2:
        picked = orch.router.pick_from(pseudo, candidates, start)
        if picked is None:
            break
        idx, pool, adapter = picked
        start = idx + 1
        if any(p.vendor == adapter.vendor for p in proposers):
            continue
        label = "AB"[len(proposers)]
        result = _call(orch, did, pseudo, wt, out, pool, adapter,
                       proposal_prompt(question, context, lang), f"proposal_{label}")
        if result is None:
            continue
        text = result.summary.strip()
        (out / f"proposal_{label}.md").write_text(text + "\n", encoding="utf-8")
        proposers.append(Proposer(label, pool, adapter.vendor, adapter, result.session_id, text))
    if not proposers:
        orch.store.update_decision(did, status="failed", error="no proposer pool gave an answer")
        raise DecideError(f"{did}: no proposer pool gave an answer (check `orch status` / breakers)")
    if len(proposers) == 1:
        orch.say(f"[{did}] 只有一家给出了方案，跳过互评")

    # 2. critiques: each proposer reviews the other's proposal
    critiques: dict[str, str] = {}  # label of the proposal -> critique of it
    if len(proposers) == 2:
        for me, other in ((proposers[0], proposers[1]), (proposers[1], proposers[0])):
            resume = me.session if (me.session and me.adapter.supports_resume()) else None
            result = _call(orch, did, pseudo, wt, out, me.pool, me.adapter,
                           critique_prompt(question, me, other, lang, resumed=bool(resume)),
                           f"critique_of_{other.label}", resume=resume)
            if result is not None:
                critiques[other.label] = result.summary.strip()
                (out / f"critique_of_{other.label}.md").write_text(critiques[other.label] + "\n", encoding="utf-8")

    # 3. judge
    title = body = ""
    judge_name = "none (orchestrator fallback)"
    start = 0
    judges = orch.cfg.role_pools("judge", "decide", "judge")
    while not body:
        picked = orch.router.pick_from(pseudo, judges, start, needs_tools=False)
        if picked is None:
            break
        idx, pool, adapter = picked
        start = idx + 1
        result = _call(orch, did, pseudo, wt, out, pool, adapter,
                       judge_prompt(question, proposers, critiques, lang), "judge")
        if result is None:
            continue
        try:
            title, body = extract_adr(result.summary)
            judge_name = f"{pool} ({adapter.vendor})"
        except PlanError as e:
            orch.say(f"[{did}] 裁判 {pool} 的回答格式不对（{e}）")
            (out / "judge_raw.md").write_text(result.summary, encoding="utf-8")
    if not body:
        title = " ".join(question.split())[:80]
        body = fallback_body(proposers, critiques)

    # 4. ADR: local copy + commit to the integration branch
    adr = header(did, title, question, proposers, judge_name) + "\n" + body.strip() + "\n"
    (out / "ADR.md").write_text(adr, encoding="utf-8")
    num = did.split("-")[-1]
    repo_rel = f"{str(dcfg.get('docs_dir') or 'docs/decisions').strip('/')}/{num}-{file_slug(title, question)}.md"
    detail = json.dumps({"proposals": {p.label: p.pool for p in proposers}, "judge": judge_name,
                         "critiques": sorted(critiques)}, ensure_ascii=False)
    try:
        sha = commit_file(orch, repo_rel, adr, f"docs: ADR {num} {title} (proposed)")
        orch.say(f"[{did}] ADR 已提交到 {orch.integration_branch}: {repo_rel}（{sha}）")
    except ws.GitError as e:
        repo_rel = ""
        orch.say(f"[{did}] ADR 没能提交到 integration 分支: {e}")
    orch.store.update_decision(did, status="proposed", title=title, adr_path=str(out / "ADR.md"),
                               repo_path=repo_rel, detail=detail)
    return did


def commit_file(orch, repo_rel: str, text: str, message: str) -> str:
    integ = orch.integration_branch
    integ_wt = ws.ensure_worktree(orch.project, orch.cfg.worktrees_dir / "_integration", integ, integ)
    ws.abort_merge(integ_wt)  # start from a clean integration worktree
    path = integ_wt / repo_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ws.git(["add", "--", repo_rel], integ_wt)
    if not ws.git(["status", "--porcelain", "--", repo_rel], integ_wt):
        return ws.git(["rev-parse", "--short", "HEAD"], integ_wt)
    ws.git([*ws.ORCH_IDENT, "commit", "--no-verify", "-q", "-m", message], integ_wt)
    sha = ws.git(["rev-parse", "--short", "HEAD"], integ_wt)
    from . import memory  # the ADR index in docs/project/DECISIONS.md follows every ADR change

    memory.refresh_adr_index(orch, Path(repo_rel).parent.as_posix())
    return sha


_STATUS_LINE = re.compile(r"^- Status: .*$", re.M)


def set_status(orch, did: str, status: str) -> str:
    """accepted / rejected: rewrite the ADR's status line locally and on the integration branch."""
    row = orch.store.get_decision(did)
    if row is None:
        raise DecideError(f"no decision {did}")
    if row["status"] not in ("proposed", "accepted", "rejected"):
        raise DecideError(f"{did} is {row['status']}; only a finished decision can be {status}")
    path = Path(row["adr_path"])
    text = path.read_text(encoding="utf-8")
    text = _STATUS_LINE.sub(f"- Status: {status} ({time.strftime('%Y-%m-%d')})", text, count=1)
    path.write_text(text, encoding="utf-8")
    msg = ""
    if row["repo_path"]:
        num = did.split("-")[-1]
        sha = commit_file(orch, row["repo_path"], text, f"docs: ADR {num} {status}")
        msg = f"{orch.integration_branch}: {row['repo_path']}（{sha}）"
    orch.store.update_decision(did, status=status)
    return msg
