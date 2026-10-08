"""Small, careful edits to a project's .agents/orch.toml (keeps the user's comments and layout).

Every edit is validated with tomllib before the file is replaced, and the old file is kept as
orch.toml.bak-<time>.
"""
from __future__ import annotations

import re
import shutil
import time
import tomllib
from pathlib import Path


def render(route: list[str]) -> str:
    return "[" + ", ".join(f'"{p}"' for p in route) + "]"


def _write(path: Path, text: str) -> Path:
    tomllib.loads(text)  # never leave a broken config behind
    backup = path.with_name(f"orch.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    return backup


def set_route(path: Path, current_routing: dict, key: str, route: list[str], note: str = "") -> Path:
    """Set `key = [...]` inside [routing]. If the file has no [routing] (the built-in defaults apply),
    write every current route out first, or the new section would replace all of them."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "[routing]"), None)
    if start is None:
        block = ["", "[routing]", "# written by orch (this project used the built-in default routes)"]
        for k, v in current_routing.items():
            if isinstance(v, dict):
                block += [f"{k}.{d} = {render(r)}" for d, r in v.items()]
            else:
                block.append(f"{k} = {render(v)}")
        lines += block
        start = len(lines) - len(block) + 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    line = f"{key} = {render(route)}" + (f"   # {note}" if note else "")
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            lines[i] = line
            break
    else:
        lines.insert(start + 1, line)
    return _write(path, "\n".join(lines) + "\n")


def add_pool(path: Path, name: str, values: dict[str, object], comment: str = "") -> Path | None:
    """Append a [pools.NAME] section unless the project already has one. Returns the backup path."""
    text = path.read_text(encoding="utf-8")
    if re.search(rf"^\[pools\.{re.escape(name)}\]\s*$", text, re.M):
        return None
    body = [f"[pools.{name}]"] + ([f"# {comment}"] if comment else [])
    for k, v in values.items():
        body.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {str(v).lower() if isinstance(v, bool) else v}")
    return _write(path, text.rstrip() + "\n\n" + "\n".join(body) + "\n")
