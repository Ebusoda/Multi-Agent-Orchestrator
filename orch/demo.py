"""`orch demo`: an offline sandbox that exercises escalation, breaker and handoff for free.

`orch probe`: M0 - one tiny real task on a real CLI, to record its event stream.
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

from . import workspace as ws
from .models import Task, TaskStatus
from .scheduler import Orchestrator, init_project
from .i18n import tr

CALC_BUGGY = '''\
def add(a, b):
    return a - b  # bug: should be a + b
'''

TEST_CALC = '''\
import unittest

import calc


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calc.add(2, 3), 5)


class TestMultiply(unittest.TestCase):
    def test_multiply(self):
        self.assertEqual(calc.multiply(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
'''

# --- fake "agents" (each is just a script run inside the task worktree) ---------------

FAKE_COMMON = '''\
import json, pathlib, sys, uuid
SID = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "{prefix}-" + uuid.uuid4().hex[:6]
PROGRESS = pathlib.Path(".task/PROGRESS.md")
def note(line):
    PROGRESS.write_text(PROGRESS.read_text(encoding="utf-8") + "\\n" + line + "\\n", encoding="utf-8")
def emit(**ev):
    ev.setdefault("session_id", SID)
    print(json.dumps(ev), flush=True)
'''

FAKE_LAZY = FAKE_COMMON.replace("{prefix}", "lazy") + '''
# Claims success without changing anything (the classic weak-model failure).
note("- [lazy agent] Looked at the code, it seems fine to me. Done.")
emit(type="result", text="Done! Everything works.")
'''

FAKE_FLAKY = FAKE_COMMON.replace("{prefix}", "flaky") + '''
import time
# Starts the work, leaves notes, then hits a rate limit mid-run.
src = pathlib.Path("calc.py")
src.write_text(src.read_text(encoding="utf-8") + "\\n# TODO multiply(): started by flaky agent\\n", encoding="utf-8")
note("- [flaky agent] In progress: started multiply(), added a TODO marker in calc.py.")
note("- [flaky agent] Rejected approach: math.prod (overkill for two numbers).")
emit(type="error", error="rate_limit: usage limit reached", resets_at=time.time() + 3600)
sys.exit(1)
'''

FAKE_GOOD = FAKE_COMMON.replace("{prefix}", "good") + '''
# Reads the task (and handoff, if any), then does exactly what the task asks.
task = json.loads(pathlib.Path(".task/TASK.json").read_text(encoding="utf-8"))
want = (task["title"] + " " + task["spec"]).lower()
if pathlib.Path(".task/HANDOFF.md").exists():
    note("- [good agent] Read HANDOFF.md first; removed the previous agent's TODO marker.")
src = pathlib.Path("calc.py")
code = "\\n".join(l for l in src.read_text(encoding="utf-8").splitlines() if "TODO multiply" not in l)
code = code.rstrip() + "\\n"
done = []
if "a - b" in code and "a + b" in want:
    code = code.replace("return a - b  # bug: should be a + b", "return a + b")
    done.append("add() now returns a + b")
if "multiply" in want and "def multiply" not in code:
    code += "\\n\\ndef multiply(a, b):\\n    return a * b\\n"
    done.append("implemented multiply()")
src.write_text(code, encoding="utf-8")
note("- [good agent] Done: " + ("; ".join(done) or "nothing needed") + ". Ran the unit tests.")
emit(type="result", text="; ".join(done) or "nothing to do")
'''

FAKE_REVIEWER = '''\
import json, pathlib, uuid
# Reviews like a strict colleague from another company: reads the diff, never edits files.
diff = pathlib.Path(".task/REVIEW_DIFF.patch").read_text(encoding="utf-8")
added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
issues = []
if any("TODO" in l for l in added):
    issues.append({"severity": "major", "file": "calc.py", "detail": "A TODO marker was left in the code."})
issues.append({"severity": "minor", "file": "calc.py", "detail": "Consider a docstring for the changed function."})
verdict = {"verdict": "approve", "summary": "Change matches the task; acceptance test covers it.", "issues": issues}
text = "I read TASK.md and the diff.\\n```json\\n" + json.dumps(verdict) + "\\n```"
print(json.dumps({"type": "result", "session_id": "rev-" + uuid.uuid4().hex[:6], "text": text}), flush=True)
'''

DEMO_CONFIG = """# Demo 配置：三个假执行者 + 一个假审查者，不花任何额度。
[project]
base_branch = ""
integration_branch = "orch/integration"
worktrees_dir = ""

[escalation]
verify_failures = 2
max_runs_per_task = 4
run_timeout_minutes = 5
breaker_threshold = 2
breaker_cooldown_minutes = 30
verify_timeout_minutes = 5

[review]
enabled = true
pools = ["fake_reviewer"]                  # 验收通过后由“另一家公司”的假审查者只读审查
max_fix_rounds = 1
skip_risk = []
require_risk = ["low", "normal", "high", "critical"]   # 演示：每个任务都审

[routing]
demo_lazy = ["fake_lazy", "fake_good"]     # 先给“偷懒”的执行者：没改任何文件、验收失败，提前升级
demo_flaky = ["fake_flaky", "fake_good"]   # 先给会被限流的执行者，熔断后交接
default = ["fake_good"]

[pools.fake_lazy]
executor = "fake"
script = ".agents/fake/lazy.py"

[pools.fake_flaky]
executor = "fake"
script = ".agents/fake/flaky.py"

[pools.fake_good]
executor = "fake"
script = ".agents/fake/good.py"

[pools.fake_reviewer]
executor = "fake"
script = ".agents/fake/reviewer.py"
vendor = "reviewer-co"                     # 和执行者不是同一家，才会被选来审查
"""


def _unittest_cmd(target: str) -> str:
    return f'"{sys.executable}" -m unittest -q {target}'


def create_demo(root: Path, say=print) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(tr("{0} 已存在且不为空，请换一个目录或先删除它。", root))
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    ws.git(["init", "-q"], root)
    ws.git(["checkout", "-q", "-b", "main"], root, check=False)
    ws.git(["add", "-A"], root)
    ws.git([*ws.ORCH_IDENT, "commit", "-q", "-m", "demo: initial (add() is buggy, multiply() missing)"], root)

    init_project(root)
    agents = root / ".agents"
    (agents / "orch.toml").write_text(DEMO_CONFIG, encoding="utf-8")
    fake = agents / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "lazy.py").write_text(FAKE_LAZY, encoding="utf-8")
    (fake / "flaky.py").write_text(FAKE_FLAKY, encoding="utf-8")
    (fake / "good.py").write_text(FAKE_GOOD, encoding="utf-8")
    (fake / "reviewer.py").write_text(FAKE_REVIEWER, encoding="utf-8")

    orch = Orchestrator(root, say=say)
    t1 = orch.store.add_task(Task(
        id="", title=tr("修复 add() 的 bug"), type="demo_lazy", difficulty="S",
        spec="calc.add(a, b) returns a - b. It must return a + b.",
        scope=["calc.py"], verify=[_unittest_cmd("test_calc.TestAdd")],
    ))
    orch.store.add_task(Task(
        id="", title=tr("实现 multiply()"), type="demo_flaky", difficulty="S",
        spec="Add calc.multiply(a, b) returning a * b.",
        scope=["calc.py"], verify=[_unittest_cmd("test_calc.TestMultiply")],
        depends_on=[t1.id],
    ))
    orch.store.close()
    return root


def run_probe(executor: str, model: str, base: Path, timeout_min: float, say=print) -> int:
    """M0: run one tiny real task with a real CLI and summarise its event stream."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = (base / f"{executor}-{stamp}").resolve()
    root.mkdir(parents=True)
    (root / "README.md").write_text("# probe repo\n", encoding="utf-8")
    ws.git(["init", "-q"], root)
    ws.git(["checkout", "-q", "-b", "main"], root, check=False)
    ws.git(["add", "-A"], root)
    ws.git([*ws.ORCH_IDENT, "commit", "-q", "-m", "probe: init"], root)
    init_project(root)
    cfg_path = root / ".agents" / "orch.toml"
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace("[routing]\n", "[routing]\nprobe = [\"probe\"]\n", 1)
    text += f'\n[pools.probe]\nexecutor = "{executor}"\nmodel = "{model}"\n'
    text = text.replace("run_timeout_minutes = 45", f"run_timeout_minutes = {timeout_min:g}")
    cfg_path.write_text(text, encoding="utf-8")

    check = (
        f'"{sys.executable}" -c "import pathlib,sys; '
        "sys.exit(0 if pathlib.Path('hello.txt').read_text(encoding='utf-8').strip()=='hello' else 1)\""
    )
    orch = Orchestrator(root, say=say)
    task = orch.store.add_task(Task(
        id="", title="Create hello.txt", type="probe", difficulty="S", max_runs=1,
        spec="Create a file named hello.txt in the repository root whose content is exactly: hello",
        scope=["hello.txt"], verify=[check],
    ))
    say(tr("probe 仓库: {0}", root))
    say(tr("正在用 {0} 运行一个极小的任务（会消耗少量额度）...", executor))
    try:
        orch.run(only=[task.id], max_tasks=1)
    except KeyboardInterrupt:
        say(tr("已中断"))
    runs = orch.store.runs_for(task.id)
    if not runs:
        say(tr("没有产生任何运行记录（执行者不可用？先运行 orch doctor）"))
        return 2
    run = runs[-1]
    types: collections.Counter[str] = collections.Counter()
    ev_path = Path(run.events_path)
    if ev_path.exists():
        for line in ev_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
                types[str(obj.get("type", "?")) if isinstance(obj, dict) else "?"] += 1
            except json.JSONDecodeError:
                types["(non-json line)"] += 1
    final = orch.store.get_task(task.id)
    say("")
    say(tr("== probe 结果 =="))
    say(f"run status : {run.status.value}   exit code: {run.exit_code}")
    say(f"verified   : {run.verified}   task: {final.status.value if final else '?'}")
    say(f"session_id : {run.session_id}")
    say(f"tokens     : in={run.tokens_in} out={run.tokens_out}   est cost: {run.est_cost_usd}")
    say(f"event types: {dict(types)}")
    say(f"events file: {ev_path}")
    say(f"stderr file: {ev_path.with_suffix('.stderr.log')}")
    if run.error:
        say(f"error      : {run.error[-500:]}")
    say(tr("把 events / stderr 两个文件发给 Claude，可以据此修正适配器的解析。"))
    orch.store.close()
    return 0 if final and final.status == TaskStatus.VERIFIED else 1


# --- handoff test: a real task for real executors (Claude -> Codex) ----------------------

TEXTSTATS_STUB = '''\
"""Small text statistics library. Implement every function so that test_textstats.py passes.

Definitions used throughout:
- A word is a run of ASCII letters, digits or apostrophes; words are compared in lowercase.
"""


def word_count(text: str) -> int:
    """Number of words in text (0 for empty text)."""
    raise NotImplementedError


def unique_words(text: str) -> set[str]:
    """Set of distinct lowercase words."""
    raise NotImplementedError


def top_words(text: str, n: int) -> list[tuple[str, int]]:
    """The n most frequent (word, count) pairs, by count descending, then word ascending."""
    raise NotImplementedError


def sentence_count(text: str) -> int:
    """Split on runs of '.', '!' or '?' and count the pieces that are not blank."""
    raise NotImplementedError


def average_word_length(text: str) -> float:
    """Mean word length rounded to 2 decimals; 0.0 when there are no words."""
    raise NotImplementedError


def longest_words(text: str, n: int) -> list[str]:
    """The n longest unique words, by length descending, then alphabetically."""
    raise NotImplementedError


def reading_time_seconds(text: str, wpm: int = 200) -> int:
    """Seconds needed to read text at wpm words per minute, rounded up."""
    raise NotImplementedError
'''

TEST_TEXTSTATS = 'import unittest\n\nimport textstats as ts\n\nSAMPLE = "The cat sat. The cat ran! Did the dog see it? Yes, the dog\'s owner did."\n\n\nclass TestWords(unittest.TestCase):\n    def test_word_count(self):\n        self.assertEqual(ts.word_count(SAMPLE), 16)\n        self.assertEqual(ts.word_count(""), 0)\n\n    def test_unique_words(self):\n        self.assertEqual(ts.unique_words("A a B b c"), {"a", "b", "c"})\n\n    def test_top_words(self):\n        self.assertEqual(ts.top_words(SAMPLE, 3), [("the", 4), ("cat", 2), ("did", 2)])\n\n    def test_longest_words(self):\n        self.assertEqual(ts.longest_words(SAMPLE, 2), ["dog\'s", "owner"])\n\n\nclass TestSentences(unittest.TestCase):\n    def test_sentence_count(self):\n        self.assertEqual(ts.sentence_count(SAMPLE), 4)\n        self.assertEqual(ts.sentence_count("No terminal punctuation"), 1)\n        self.assertEqual(ts.sentence_count("   "), 0)\n\n\nclass TestNumbers(unittest.TestCase):\n    def test_average_word_length(self):\n        self.assertEqual(ts.average_word_length("ab abcd"), 3.0)\n        self.assertEqual(ts.average_word_length(""), 0.0)\n        self.assertEqual(ts.average_word_length(SAMPLE), 3.19)\n\n    def test_reading_time(self):\n        self.assertEqual(ts.reading_time_seconds("word " * 200), 60)\n        self.assertEqual(ts.reading_time_seconds("word " * 201), 61)\n        self.assertEqual(ts.reading_time_seconds("one two", wpm=60), 2)\n\n\nif __name__ == "__main__":\n    unittest.main()\n'


def create_handoff_test(root: Path) -> Path:
    """A repo with one medium task routed Claude -> Codex, for a real interrupt/handoff test."""
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(tr("{0} 已存在且不为空，请换一个目录或先删除它。", root))
    root.mkdir(parents=True, exist_ok=True)
    (root / "textstats.py").write_text(TEXTSTATS_STUB, encoding="utf-8")
    (root / "test_textstats.py").write_text(TEST_TEXTSTATS, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    ws.git(["init", "-q"], root)
    ws.git(["checkout", "-q", "-b", "main"], root, check=False)
    ws.git(["add", "-A"], root)
    ws.git([*ws.ORCH_IDENT, "commit", "-q", "-m", "handoff test: textstats stub + tests"], root)
    init_project(root)
    cfg = root / ".agents" / "orch.toml"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("[routing]\n", tr("[routing]\nhandoff_test = [\"claude_sub\", \"codex_sub\"]   # 交接测试：先 Claude，后 Codex\n"), 1)
    cfg.write_text(text, encoding="utf-8")
    orch = Orchestrator(root, say=lambda _m: None)
    orch.store.add_task(Task(
        id="", title=tr("实现 textstats.py"), type="handoff_test", difficulty="M",
        spec="Implement every function in textstats.py (see the docstrings) so that all tests in "
             "test_textstats.py pass. Do not change the tests.",
        scope=["textstats.py"], verify=[f'"{sys.executable}" -m unittest test_textstats'],
    ))
    orch.store.close()
    return root
