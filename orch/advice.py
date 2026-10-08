"""v0.3: routing suggestions from the execution records. Statistics -> suggestion -> the user approves.

Nothing changes on its own: `orch route suggest` prints suggestions, `orch route apply S-n` edits
the routing line in .agents/orch.toml (a backup is kept). Personal task volume is small, so every
rule needs a minimum sample (default 5 tasks started on the pool) before it says anything.

Rules, per routing key (task type, difficulty) and the pool its route starts on:
  drop-first  half or more of the tasks started there had to escalate -> start on the next pool
  keep        80% or more passed on the first run -> the route is doing its job
  otherwise   no suggestion (mixed results; keep collecting data)
"""
from __future__ import annotations

import re
import shutil
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .report import build_report

MIN_SAMPLES = 5


@dataclass
class Advice:
    id: str                         # S-1, S-2 ... (only for suggestions that change something)
    key: str                        # routing key, e.g. code_change.S
    route: list[str]
    action: str                     # drop-first | keep | wait
    reason: str
    new_route: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def toml_line(self) -> str:
        return f"{self.key} = [{', '.join(chr(34) + p + chr(34) for p in self.new_route)}]"


def _route_key(cfg, task_type: str, difficulty: str) -> str:
    """The key in [routing] that decides this task's route (what an edit has to change)."""
    by_type = cfg.data["routing"].get(task_type)
    if isinstance(by_type, dict):
        return f"{task_type}.{difficulty}" if difficulty in by_type else f"{task_type}.default"
    if isinstance(by_type, list):
        return task_type
    return "default"


def suggest(orch, min_samples: int | None = None) -> list[Advice]:
    min_n = int(min_samples or orch.cfg.data.get("advice", {}).get("min_samples", MIN_SAMPLES))
    executors = {p: str(c.get("executor", "")) for p, c in orch.cfg.pools.items()}
    rep = build_report(orch.store, orch.cfg.agents_dir, executors)
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in rep["tasks"]:
        if t["attempts"]:
            by_key[(t["type"], t["difficulty"])].append(t)

    out: list[Advice] = []
    n_id = 0
    for (ttype, diff), tasks in sorted(by_key.items()):
        route = orch.cfg.route_for(ttype, diff)
        key = _route_key(orch.cfg, ttype, diff)
        if not route:
            continue
        head = route[0]
        started = [t for t in tasks if next(iter(t["attempts"])) == head]
        n = len(started)
        first = sum(1 for t in started if t["first_pass"])
        escalated = sum(1 for t in started if t["pool"] != head)
        stats = {"tasks": len(tasks), "started_on_first_pool": n, "first_pass": first, "escalated": escalated}
        label = f"{ttype}.{diff}"
        if n < min_n:
            out.append(Advice("", key, route, "wait", f"{label}：从 {head} 起步的任务只有 {n} 个，"
                              f"至少要 {min_n} 个才给建议", stats=stats))
        elif escalated * 2 >= n and len(route) > 1:
            n_id += 1
            out.append(Advice(f"S-{n_id}", key, route, "drop-first",
                              f"{label}：从 {head} 起步的 {n} 个任务里有 {escalated} 个升级到了后面的池，"
                              f"直接从 {route[1]} 起步可以省掉失败的那几轮", new_route=route[1:], stats=stats))
        elif first * 5 >= n * 4:
            out.append(Advice("", key, route, "keep", f"{label}：{head} 首次通过 {first}/{n}，保持现在的路由",
                              stats=stats))
        else:
            out.append(Advice("", key, route, "wait", f"{label}：{head} 首次通过 {first}/{n}、升级 {escalated}/{n}，"
                              "结果不一致，继续观察", stats=stats))
    return out


# -- applying a suggestion (only on the user's command) --------------------------------

def _render(value: Any) -> str:
    return "[" + ", ".join(f'"{v}"' for v in value) + "]"


def apply(orch, advice_id: str) -> tuple[Path, str]:
    """Write the suggested route into .agents/orch.toml. Returns (backup path, new line)."""
    adv = next((a for a in suggest(orch) if a.id == advice_id), None)
    if adv is None:
        raise ValueError(f"没有建议 {advice_id}（建议是按当前数据重新算的，先看 orch route suggest）")
    path = orch.cfg.agents_dir / "orch.toml"
    text = path.read_text(encoding="utf-8")
    line = adv.toml_line()
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "[routing]"), None)
    if start is None:
        # routing comes from the built-in defaults: write them all out, or the new section would
        # replace every default route with this single line
        block = ["", "[routing]", "# 由 orch route apply 写出（原来使用内置默认路由）"]
        for k, v in orch.cfg.data["routing"].items():
            if isinstance(v, dict):
                block += [f"{k}.{d} = {_render(r)}" for d, r in v.items()]
            else:
                block.append(f"{k} = {_render(v)}")
        lines += block
        start = len(lines) - len(block) + 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(rf"^\s*{re.escape(adv.key)}\s*=")
    stamp = f"   # {time.strftime('%Y-%m-%d')} orch route apply {advice_id}（原为 {_render(adv.route)}）"
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            lines[i] = line + stamp
            break
    else:
        lines.insert(start + 1, line + stamp)
    new_text = "\n".join(lines) + "\n"
    tomllib.loads(new_text)  # never leave a broken config behind
    backup = path.with_name(f"orch.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
    orch.store.log("route_applied", f"{advice_id}: {line} (was {_render(adv.route)})")
    return backup, line
