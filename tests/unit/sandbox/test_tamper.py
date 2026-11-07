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
    rep = scan("import inspect\nprint(inspect.stack())\n")
    assert rep.forgery == ()
    assert "inspect" in rep.info


def test_scan_clean_solution() -> None:
    assert scan("def add(a, b):\n    return a + b\n") == TamperReport(forgery=(), info=())


@pytest.mark.parametrize(
    "code",
    [
        "__import__('os')._exit(0)",
        "import os\nos.kill(1, 9)",
        "import os\nos.killpg(1, 9)",
        "import signal\nsignal.raise_signal(9)\nx.kill()",
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
        "def f(:\n",
    ],
)
def test_scan_flags_forgery_variants(code: str) -> None:
    assert scan(code).forgery


@pytest.mark.parametrize(
    ("code", "tag"),
    [
        ("import sys\nsys.exit(0)", "sys.exit"),
        ("import gc\ngc.get_objects()", "gc.get_objects"),
        ("open('test_hidden.py').read()", "open_test_file"),
        ("from inspect import stack", "inspect"),
    ],
)
def test_scan_info_patterns(code: str, tag: str) -> None:
    rep = scan(code)
    assert rep.forgery == ()
    assert tag in rep.info


def test_syntax_error_is_forgery_syntax_error() -> None:
    assert scan("def add(:\n") == TamperReport(forgery=("syntax_error",), info=())


def test_scan_never_executes_code() -> None:
    assert scan("raise SystemExit(5)\n").forgery == ()


def test_open_non_test_file_is_clean() -> None:
    assert scan("open('data.txt')\nopen('test_x.txt')\n").info == ()
