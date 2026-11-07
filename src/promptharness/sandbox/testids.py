"""Expected test IDs for the manifest check, derived by AST only (the file is never imported)."""

from __future__ import annotations

import ast


def collect_test_ids(filename: str, source: str) -> list[str]:
    """Return sorted, deduplicated node bases such as ``test_hidden.py::TestX::test_y``.

    Collects module-level ``test_*`` functions and ``test_*`` methods of ``Test*`` classes.
    A file that does not parse yields ``[]``.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError):
        return []
    ids: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test_"):
                ids.add(f"{filename}::{node.name}")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(
                    item, ast.FunctionDef | ast.AsyncFunctionDef
                ) and item.name.startswith("test_"):
                    ids.add(f"{filename}::{node.name}::{item.name}")
    return sorted(ids)
