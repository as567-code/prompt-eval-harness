"""Tests for scripts/gates.py (grep-style security gates)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATES = REPO / "scripts" / "gates.py"


def run_gates(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(GATES), str(root)],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def test_gates_clean_repo() -> None:
    result = run_gates(REPO)
    assert result.returncode == 0, result.stdout + result.stderr


CASES = [
    ("no-shell-true", "src/mod.py", "subprocess.run(x, shell=True)\n"),
    ("no-shell-true", "scripts/tool.py", "subprocess.run(x, shell=True)\n"),
    ("no-unsafe-yaml", "src/mod.py", "data = yaml.load(f)\n"),
    ("no-unsafe-yaml", "src/mod.py", "x = thing(Loader=yaml.Loader)\n"),
    ("no-pickle", "src/mod.py", "import pickle\n"),
    ("no-pickle", "src/mod.py", "x = pickle.loads(b)\n"),
    ("no-pickle", "src/mod.py", "x = marshal.loads(b)\n"),
    ("no-eval-exec", "src/mod.py", "x = eval(s)\n"),
    ("no-eval-exec", "src/mod.py", "exec(s)\n"),
    ("no-cli-in-ci", ".github/workflows/ci.yml", "      - run: claude -p hi\n"),
    ("no-cli-in-ci", ".github/workflows/ci.yml", "      - run: codex exec hi\n"),
    ("no-cli-in-ci", ".github/workflows/ci.yml", "  ANTHROPIC_" + "KEY: x\n"),
    ("no-cli-in-ci", ".github/workflows/ci.yml", "  OPENAI_" + "KEY: x\n"),
    ("no-bare-or-fallback", "src/mod.py", 'args = ["--bare"]\n'),
    ("no-bare-or-fallback", "src/mod.py", 'args = ["--fallback-model"]\n'),
    ("no-credential-read", "src/mod.py", 'p = home / "auth.json"\n'),
    ("no-credential-read", "src/mod.py", 'p = home / ".credentials"\n'),
    ("no-credential-read", "src/mod.py", "k = keychain\n"),
    ("no-credential-read", "src/mod.py", "security find-generic-password -s x\n"),
]


@pytest.mark.parametrize(("rule", "relpath", "content"), CASES)
def test_gates_detects_each_rule(tmp_path: Path, rule: str, relpath: str, content: str) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    result = run_gates(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{relpath}:1: {rule}" in result.stdout


def test_gates_scoped_rules_ignore_other_dirs(tmp_path: Path) -> None:
    """eval/bare rules are src-only; tests may mention them."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text('eval(x)\nargs = ["--bare"]\n')
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "s.py").write_text("eval(x)\n")
    assert run_gates(tmp_path).returncode == 0


def test_gates_clean_tree_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("x = 1\n")
    result = run_gates(tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
