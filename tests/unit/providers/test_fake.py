import pytest

from promptharness.providers.base import (
    CliError,
    CliTimeout,
    CompletionRequest,
    InvalidRequestError,
    ProviderError,
    ServedModelMismatch,
    Status,
    ToolUseDetected,
    UsageLimitError,
)
from promptharness.providers.fake import FakeProvider
from promptharness.usage import Usage


def _req(**kw: object) -> CompletionRequest:
    base: dict[str, object] = {"model": "m", "effort": "low", "system": "s", "prompt": "p"}
    base.update(kw)
    return CompletionRequest(**base)  # type: ignore[arg-type]


def test_fake_returns_script_in_order() -> None:
    p = FakeProvider([FakeProvider.text("a"), FakeProvider.text("b")])
    r1, r2 = _req(prompt="1"), _req(prompt="2")
    assert p.complete(r1).text == "a"
    assert p.complete(r2).text == "b"
    assert p.requests == [r1, r2]
    with pytest.raises(IndexError, match="FakeProvider script exhausted"):
        p.complete(r1)


def test_text_builder_defaults() -> None:
    c = FakeProvider.text("hi")
    assert c.status is Status.OK
    assert c.usage_gross == Usage(input=10, output=10)
    assert c.served_model == "fake-model"
    assert c.raw == "hi"
    assert c.provider == "fake"
    assert c.cli_version == "fake"
    assert (c.cli_attempts, c.tool_attempts, c.latency_ms_wall) == (1, 0, 0)
    assert c.structured is None
    assert c.stop_reason == "end_turn"
    assert c.output_cap_applied is False


def test_fake_raises_scripted_error() -> None:
    p = FakeProvider([CliTimeout("slow")])
    with pytest.raises(CliTimeout):
        p.complete(_req())
    assert len(p.requests) == 1


def test_request_validation() -> None:
    _req().validate()
    with pytest.raises(InvalidRequestError):
        _req(prompt="a\x00b").validate()
    with pytest.raises(InvalidRequestError):
        _req(system="a\x00b").validate()
    _req(prompt="x" * 200_000).validate()
    with pytest.raises(InvalidRequestError):
        _req(prompt="x" * 200_001).validate()
    with pytest.raises(InvalidRequestError):
        _req(prompt="é" * 100_000 + "x").validate()
    with pytest.raises(InvalidRequestError):
        _req(effort="ultra").validate()
    with pytest.raises(InvalidRequestError):
        _req(timeout_s=0).validate()
    with pytest.raises(InvalidRequestError):
        _req(max_output_tokens=0).validate()
    _req(max_output_tokens=1).validate()


def test_error_classes() -> None:
    assert UsageLimitError("x", reset_hint=None).error_class == "usage_limit"
    assert ToolUseDetected("x").error_class == "tool_use_detected"
    assert InvalidRequestError("x").error_class == "cli_error"
    assert CliTimeout("x").error_class == "timeout"
    e = CliError("boom", transient=True)
    assert e.error_class == "cli_error" and e.transient and e.attempts == 1
    m = ServedModelMismatch("a", "b")
    assert (m.requested, m.served, m.error_class) == ("a", "b", "served_model_mismatch")
    assert isinstance(m, ProviderError)
    assert UsageLimitError("x", reset_hint="5pm").reset_hint == "5pm"
