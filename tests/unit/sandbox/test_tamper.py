"""Static forgery scanner tests: the scan walks the AST and never runs the code."""

import pytest

from promptharness.sandbox.tamper import TamperReport, scan


def test_scan_flags_os_exit() -> None:
    rep = scan("import os\nos._exit(0)\n")
    assert rep.forgery
    assert any("_exit" in p for p in rep.forgery)


def test_scan_flags_pytest_import() -> None:
    assert scan("import pytest\n").forgery
    assert scan("from _pytest import config\n").forgery


def test_scan_flags_sys_modules_assignment() -> None:
    rep = scan("import sys\nsys.modules['x'] = object()\n")
    assert rep.forgery


def test_scan_flags_report_xml_literal() -> None:
    assert scan("open('/work/report.xml', 'w')\n").forgery


def test_scan_info_only_inspect() -> None:
    rep = scan("import inspect\nprint(inspect.signature(len))\n")
    assert rep.forgery == ()
    assert "inspect" in rep.info


def test_scan_clean_solution() -> None:
    assert scan("def add(a, b):\n    return a + b\n") == TamperReport(forgery=(), info=())


@pytest.mark.parametrize(
    "code",
    [
        "__import__('os')._exit(0)",
        "import os\nos.kill(1, 9)",
        "import os as o\no.kill(1, 9)",
        "from os import kill\nkill(1, 9)",
        "from os import killpg as kp\nkp(1, 9)",
        "__import__('os').kill(1, 9)",
        "import sys as s\ns.modules['a'] = 1",
        'exec("import os; os._exit(0)")',
        "eval('1')",
        "compile('x', 'f', 'exec')",
        "import builtins\nbuiltins.exec('1')",
        "__builtins__.eval('1')",
        "x = 'junit'",
        "open('/work/report.xml', 'w')",
        "import os\nos.killpg(1, 9)",
        "import ctypes",
        "from faulthandler import dump_traceback",
        "import os, pytest",
        "__import__('pytest')",
        "import importlib\nimportlib.import_module('ctypes')",
        "import sys\nsys.modules['pytest'] = None",
        "import sys\nsys.modules.__setitem__('a', 1)",
        "import sys\nsys.modules.update({})",
        "import sys\ndel sys.modules['a']",
        "x = 'JUnitXML'",
        "x = '/work/Report.XML'",
        "from os import _exit",
    ],
)
def test_scan_flags_forgery_variants(code: str) -> None:
    assert scan(code).forgery


@pytest.mark.parametrize(
    ("code", "tag"),
    [
        ("import sys\nsys.exit(0)", "sys.exit"),
        ("open('test_hidden.py').read()", "open_test_file"),
        ("from inspect import signature", "inspect"),
    ],
)
def test_scan_info_patterns(code: str, tag: str) -> None:
    rep = scan(code)
    assert rep.forgery == ()
    assert tag in rep.info


def test_syntax_error_is_info_not_forgery() -> None:
    assert scan("def add(:\n") == TamperReport(forgery=(), info=("syntax_error",))


@pytest.mark.parametrize(
    "code",
    [
        "def f(proc):\n    proc.kill()",
        "class A:\n    def g(self):\n        self.kill()",
        '"""Parses JUnit-style results."""\nx = 1',
        'def f():\n    """JUnit helper."""\n',
        'class C:\n    """junit"""\n',
        '"junit"\nx = 1',
        "import os\nos.getpid()",
    ],
)
def test_scan_false_positives_are_clean(code: str) -> None:
    assert scan(code).forgery == ()


def test_scan_uses_the_bytes_cpython_would_run() -> None:
    latin1 = b"# -*- coding: latin-1 -*-\nx = '\xe9'\nimport os\nos._exit(0)\n"
    assert "attr:_exit" in scan(latin1).forgery
    assert "attr:_exit" in scan("\ufeffimport os\nos._exit(0)\n").forgery
    assert "attr:_exit" in scan(b"\xef\xbb\xbfimport os\nos._exit(0)\n").forgery
    assert scan(b"\xff\xfe\x00bad").info == ("syntax_error",)


@pytest.mark.parametrize(
    "code",
    [
        'import os\nos.execv("/bin/true", ["true"])',
        "import os\nos.execvpe('a', [], {})",
        "from os import execv\nexecv('a', [])",
        "x.execlp('a', 'a')",
        "import sys\nsys._getframe(1)",
        "import inspect\ninspect.currentframe()",
        "import inspect as i\ni.stack()",
        "from inspect import trace",
        "import gc\ngc.get_objects()",
        "import gc\ngc.get_referrers(1)",
        "import gc as g\ng.get_referents(1)",
    ],
)
def test_scan_flags_clean_exit_and_introspection_paths(code: str) -> None:
    assert scan(code).forgery


def test_inspect_signature_stays_info_only() -> None:
    rep = scan("import inspect\ninspect.signature(len)")
    assert rep.forgery == ()
    assert "inspect" in rep.info


def test_scan_never_executes_code() -> None:
    assert scan("raise SystemExit(5)\n").forgery == ()


def test_open_non_test_file_is_clean() -> None:
    assert scan("open('data.txt')\nopen('test_x.txt')\n").info == ()
