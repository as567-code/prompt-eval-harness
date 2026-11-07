"""Parse pytest JUnit XML (untrusted, candidate-influenced) into test outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from defusedxml import ElementTree as SafeET

Outcome = Literal["passed", "failed", "error", "skipped"]

COLLECTION_PREFIX = "<collection>::"
_PARAM_RE = re.compile(r"\[.*\]$", re.DOTALL)


class JunitError(ValueError):
    """Raised when the report is malformed or uses forbidden XML constructs."""


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # not a pytest class

    node_base: str
    name: str
    outcome: Outcome


def _node_base(classname: str, name: str) -> str:
    base_name = _PARAM_RE.sub("", name)
    if not classname:
        # Collection/import errors are reported without a classname.
        return f"{COLLECTION_PREFIX}{base_name}"
    parts = classname.split(".")
    module_end = 0
    for i, part in enumerate(parts):
        if part.startswith("test_"):
            module_end = i
    filename = "/".join(parts[: module_end + 1]) + ".py"
    classes = parts[module_end + 1 :]
    return "::".join([filename, *classes, base_name])


def parse_junit(xml_text: str) -> list[TestOutcome]:
    """Parse ``xml_text``; raise ``JunitError`` on malformed XML or entity/DTD tricks."""
    try:
        root = SafeET.fromstring(xml_text, forbid_dtd=True)
        outcomes: list[TestOutcome] = []
        for case in root.iter("testcase"):
            name = case.get("name") or ""
            classname = case.get("classname") or ""
            tags = {child.tag for child in case}
            outcome: Outcome
            if "error" in tags:
                outcome = "error"
            elif "failure" in tags:
                outcome = "failed"
            elif "skipped" in tags:
                outcome = "skipped"
            else:
                outcome = "passed"
            outcomes.append(TestOutcome(_node_base(classname, name), name, outcome))
    except Exception as exc:
        raise JunitError(f"{type(exc).__name__}: {exc}"[:300]) from exc
    return outcomes
