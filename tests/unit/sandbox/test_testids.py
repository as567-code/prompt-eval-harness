from promptharness.sandbox.testids import collect_test_ids

SRC = """
import pytest

def helper():
    pass

def test_one():
    pass

async def test_async():
    pass

@pytest.mark.parametrize("x", [1, 2])
def test_one(x):
    pass

class TestX:
    def test_y(self):
        pass
    def helper(self):
        pass

class Helper:
    def test_ignored(self):
        pass
"""


def test_collect_test_ids() -> None:
    assert collect_test_ids("test_hidden.py", SRC) == [
        "test_hidden.py::TestX::test_y",
        "test_hidden.py::test_async",
        "test_hidden.py::test_one",
    ]


def test_collect_test_ids_syntax_error_is_empty() -> None:
    assert collect_test_ids("test_hidden.py", "def test_(:\n") == []


def test_collect_test_ids_never_imports() -> None:
    assert collect_test_ids("test_x.py", "raise SystemExit(1)\ndef test_a(): pass\n") == [
        "test_x.py::test_a"
    ]
