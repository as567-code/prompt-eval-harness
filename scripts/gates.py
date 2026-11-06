"""Grep-style security gates. Usage: python scripts/gates.py [ROOT].

Exits 0 when clean, 1 when any violation is found, printing ``path:line: rule``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

SELF = Path(__file__).resolve()


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    globs: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule("no-shell-true", re.compile(r"shell\s*=\s*True"), ("src/**/*.py", "scripts/**/*.py")),
    Rule(
        "no-unsafe-yaml",
        re.compile(r"yaml\.load\(|Loader="),
        ("src/**/*.py", "scripts/**/*.py"),
    ),
    Rule(
        "no-pickle",
        re.compile(r"import pickle|pickle\.|marshal\."),
        ("src/**/*.py", "scripts/**/*.py"),
    ),
    Rule("no-eval-exec", re.compile(r"\beval\(|\bexec\("), ("src/**/*.py",)),
    Rule(
        "no-cli-in-ci",
        re.compile(r"claude |codex |ANTHROPIC_|OPENAI_"),
        (".github/workflows/*.yml", ".github/workflows/*.yaml"),
    ),
    Rule(
        "no-bare-or-fallback",
        re.compile(r"--bare|--fallback-model"),
        ("src/**/*.py",),
    ),
    Rule(
        "no-credential-read",
        re.compile(r"auth\.json|\.credentials|keychain|security find-generic-password", re.I),
        ("src/**/*.py",),
    ),
)


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for rule in RULES:
        files: set[Path] = set()
        for glob in rule.globs:
            files.update(p for p in root.glob(glob) if p.is_file())
        for path in sorted(files):
            if path.resolve() == SELF:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rule.pattern.search(line):
                    findings.append(f"{path.relative_to(root).as_posix()}:{lineno}: {rule.name}")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    findings = scan(root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
