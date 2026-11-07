"""Parity test: the in-image runner and the host must accept/reject the same member names.

Both ``_validate_name`` functions gate tar member names. ``sandbox/runner.py`` is trusted
in-image code loaded by path; the host side is imported from the package.
"""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from promptharness.sandbox.docker_runner import _validate_name as host_validate_name

RUNNER_PATH = Path(__file__).resolve().parents[3] / "sandbox" / "runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("peh_runner_parity", RUNNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()
runner_validate_name: Callable[[str], None] = runner._validate_name

VALIDATORS = [
    pytest.param(host_validate_name, id="host"),
    pytest.param(runner_validate_name, id="runner"),
]

REJECT = [
    "/abs.py",
    "../x.py",
    "a/../b.py",
    ".",
    "",
    "-rf.py",
    "report.xml",
    "d/report.xml/x.py",
    "test_hidden.TestX.py",
    "a.b/test_x.py",
    "x" * 300 + ".py",
    "a/" + "y" * 120 + ".py",
    "bad name.py",
    "ünï.py",
]

ACCEPT = [
    "solution.py",
    "test_hidden.py",
    "context/helpers.py",
    "context/data.txt",
]


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("name", REJECT)
def test_both_validators_reject(validate: Callable[[str], None], name: str) -> None:
    with pytest.raises(ValueError):
        validate(name)


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("name", ACCEPT)
def test_both_validators_accept(validate: Callable[[str], None], name: str) -> None:
    validate(name)
