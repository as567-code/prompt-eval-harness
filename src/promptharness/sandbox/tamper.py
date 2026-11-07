"""Static forgery scan of candidate code (PRD 5.5). Walks the AST; never runs the code.

Best-effort by design: deliberately obfuscated forgery (for example a constructed ``getattr``)
is not caught here. Host containment and the manifest check are the hard guarantees.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["TamperReport", "scan"]

_FORBIDDEN_ATTRS = frozenset({"_exit", "kill", "killpg"})
_FORBIDDEN_MODULES = frozenset({"pytest", "_pytest", "ctypes", "faulthandler"})
_FORBIDDEN_STRINGS = ("junit", "report.xml")
_MODULES_MUTATORS = frozenset({"update", "__setitem__", "__delitem__", "setdefault", "pop"})


@dataclass(frozen=True)
class TamperReport:
    forgery: tuple[str, ...]
    info: tuple[str, ...]


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.forgery: set[str] = set()
        self.info: set[str] = set()

    def _module(self, name: str) -> None:
        root = name.split(".")[0]
        if root in _FORBIDDEN_MODULES:
            self.forgery.add(f"import:{root}")
        if root == "inspect":
            self.info.add("inspect")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._module(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._module(node.module)
        for alias in node.names:
            if alias.name in _FORBIDDEN_ATTRS:
                self.forgery.add(f"attr:{alias.name}")
            if node.module == "sys" and alias.name == "modules":
                self.forgery.add("sys.modules")
            if node.module is None or node.module == "":
                self._module(alias.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _FORBIDDEN_ATTRS:
            self.forgery.add(f"attr:{node.attr}")
        if isinstance(node.value, ast.Name):
            base = node.value.id
            if base == "sys" and node.attr == "exit":
                self.info.add("sys.exit")
            if base == "gc" and node.attr == "get_objects":
                self.info.add("gc.get_objects")
            if base == "inspect":
                self.info.add("inspect")
            if base == "sys" and node.attr == "modules" and isinstance(node.ctx, ast.Store):
                self.forgery.add("sys.modules")
        if _is_sys_modules(node.value) and node.attr in _MODULES_MUTATORS:
            self.forgery.add("sys.modules")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "inspect":
            self.info.add("inspect")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_sys_modules(node.value) and isinstance(node.ctx, ast.Store | ast.Del):
            self.forgery.add("sys.modules")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            low = node.value.lower()
            for needle in _FORBIDDEN_STRINGS:
                if needle in low:
                    self.forgery.add(f"string:{needle}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        first = _const_str(node.args[0]) if node.args else None
        if first is not None:
            is_import_call = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}
            )
            if is_import_call:
                self._module(first)
            if (
                isinstance(func, ast.Name)
                and func.id == "open"
                and first.startswith("test_")
                and first.endswith(".py")
            ):
                self.info.add("open_test_file")
        self.generic_visit(node)


def scan(code: str) -> TamperReport:
    """Return forgery (verdict-changing) and info (diagnostic only) findings for ``code``."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return TamperReport(forgery=("syntax_error",), info=())
    scanner = _Scanner()
    try:
        scanner.visit(tree)
    except RecursionError:
        return TamperReport(forgery=("unscannable",), info=())
    return TamperReport(forgery=tuple(sorted(scanner.forgery)), info=tuple(sorted(scanner.info)))
