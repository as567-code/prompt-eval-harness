import pytest

from promptharness.sandbox.junit import JunitError, TestOutcome, parse_junit

XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="5">
<testcase classname="test_hidden" name="test_a" time="0.001"/>
<testcase classname="test_hidden" name="test_p[1-2]" time="0.001"/>
<testcase classname="test_hidden" name="test_p[3-4]"><failure message="x">tb</failure></testcase>
<testcase classname="test_hidden.TestX" name="test_m"><skipped message="s"/></testcase>
<testcase classname="context.test_c.TestY" name="test_e"><error message="e">tb</error></testcase>
</testsuite></testsuites>"""


def test_parse_junit_basic() -> None:
    out = parse_junit(XML)
    assert out == [
        TestOutcome("test_hidden.py::test_a", "test_a", "passed"),
        TestOutcome("test_hidden.py::test_p", "test_p[1-2]", "passed"),
        TestOutcome("test_hidden.py::test_p", "test_p[3-4]", "failed"),
        TestOutcome("test_hidden.py::TestX::test_m", "test_m", "skipped"),
        TestOutcome("context/test_c.py::TestY::test_e", "test_e", "error"),
    ]


def test_parse_junit_collection_error_has_no_classname() -> None:
    xml = (
        '<testsuites><testsuite><testcase classname="" name="test_hidden"><error/></testcase>'
        "</testsuite></testsuites>"
    )
    (o,) = parse_junit(xml)
    assert o.node_base.startswith("<collection>::")
    assert o.outcome == "error"


def test_parse_junit_rejects_entities() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        ']><testsuites><testsuite><testcase classname="a" name="&lol2;"/></testsuite></testsuites>'
    )
    with pytest.raises(JunitError):
        parse_junit(bomb)


@pytest.mark.parametrize("bad", ["", "not xml", "<a><b></a>", "<!DOCTYPE x><x/>"])
def test_parse_junit_rejects_malformed(bad: str) -> None:
    with pytest.raises(JunitError):
        parse_junit(bad)
