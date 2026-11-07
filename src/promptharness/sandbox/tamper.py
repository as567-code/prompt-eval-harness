"""Static forgery scan of candidate code (PRD 5.5). Walks the AST; never runs the code.

Best-effort by design: deliberately obfuscated forgery (for example a constructed ``getattr``)
is not caught here. Host containment and the manifest check are the hard guarantees.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["TamperReport", "scan"]

_FORBIDDEN_MODULES = frozenset({"pytest", "_pytest", "ctypes", "faulthandler"})
_OS_KILLS = frozenset({"kill", "killpg"})
_DYNAMIC = frozenset({"exec", "eval", "compile"})
_BUILTINS_BASES = frozenset({"builtins", "__builtins__"})
_MODULES_MUTATORS = frozenset({"update", "__setitem__", "__delitem__", "setdefault", "pop"})


@dataclass(frozen=True)
class TamperReport:
    forgery: tuple[str, ...]
    info: tuple[str, ...]


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_aliases(tree: ast.AST, module: str) -> set[str]:
    """Local names bound to ``module`` by ``import module`` / ``import module as x``."""
    names = {module}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module and alias.asname:
                    names.add(alias.asname)
    return names


def _docstring_like_ids(tree: ast.AST) -> set[int]:
    """Ids of string constants used as bare expression statements (docstrings included)."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and _const_str(node.value) is not None
    }


class _Scanner(ast.NodeVisitor):
    def __init__(self, tree: ast.AST) -> None:
        self.forgery: set[str] = set()
        self.info: set[str] = set()
        self._os = _module_aliases(tree, "os")
        self._sys = _module_aliases(tree, "sys")
        self._doc_ids = _docstring_like_ids(tree)

    def _is_os(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self._os
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and bool(node.args)
            and _const_str(node.args[0]) == "os"
        )

    def _is_sys_modules(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id in self._sys
        )

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
            if alias.name == "_exit":
                self.forgery.add("attr:_exit")
            if node.module == "os" and alias.name in _OS_KILLS:
                self.forgery.add(f"attr:{alias.name}")
            if node.module == "sys" and alias.name == "modules":
                self.forgery.add("sys.modules")
            if not node.module:
                self._module(alias.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "_exit":
            self.forgery.add("attr:_exit")
        if node.attr in _OS_KILLS and self._is_os(node.value):
            self.forgery.add(f"attr:{node.attr}")
        if isinstance(node.value, ast.Name):
            base = node.value.id
            if base in self._sys and node.attr == "exit":
                self.info.add("sys.exit")
            if base == "gc" and node.attr == "get_objects":
                self.info.add("gc.get_objects")
            if base == "inspect":
                self.info.add("inspect")
            if base in self._sys and node.attr == "modules" and isinstance(node.ctx, ast.Store):
                self.forgery.add("sys.modules")
        if self._is_sys_modules(node.value) and node.attr in _MODULES_MUTATORS:
            self.forgery.add("sys.modules")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "inspect":
            self.info.add("inspect")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_sys_modules(node.value) and isinstance(node.ctx, ast.Store | ast.Del):
            self.forgery.add("sys.modules")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            low = node.value.lower()
            if "report.xml" in low:
                self.forgery.add("string:report.xml")
            if "junit" in low and id(node) not in self._doc_ids:
                self.forgery.add("string:junit")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        bare = isinstance(func, ast.Name) and func.id in _DYNAMIC
        via_builtins = (
            isinstance(func, ast.Attribute)
            and func.attr in _DYNAMIC
            and isinstance(func.value, ast.Name)
            and func.value.id in _BUILTINS_BASES
        )
        if bare or via_builtins:
            self.forgery.add("dynamic_exec")
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
    """Return forgery (verdict-changing) and info (diagnostic only) findings for ``code``.

    ``unscannable`` (RecursionError) is forgery on purpose: such code parses, so it does run.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        # Code that does not parse cannot run, so it cannot forge anything (R38).
        return TamperReport(forgery=(), info=("syntax_error",))
    scanner = _Scanner(tree)
    try:
        scanner.visit(tree)
    except RecursionError:
        return TamperReport(forgery=("unscannable",), info=())
    return TamperReport(forgery=tuple(sorted(scanner.forgery)), info=tuple(sorted(scanner.info)))
