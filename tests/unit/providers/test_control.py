from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from promptharness.providers.base import (
    AuthModeError,
    CliError,
    CliTimeout,
    CompletionRequest,
    ServedModelMismatch,
    ToolUseDetected,
    UsageLimitError,
)
from promptharness.providers.control import (
    QUOTA_PAUSE_THRESHOLD,
    BudgetExceeded,
    LaneGate,
    LaneTimeout,
    Overhead,
    RunBudget,
    calibrate_overhead,
    call_with_retry,
    quota_pressure,
)
from promptharness.providers.fake import FakeProvider
from promptharness.usage import Usage

REQ = CompletionRequest(model="m", effort="low", system="", prompt="hi")
FIX = Path(__file__).parents[2] / "fixtures" / "cli"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


def test_retry_transient_then_ok() -> None:
    p = FakeProvider([CliError("blip", transient=True), FakeProvider.text("ok")])
    sleep = Sleeps()
    c = call_with_retry(p, REQ, sleep=sleep, rand=lambda: 0.5)
    assert c.cli_attempts == 2
    assert c.text == "ok"
    assert sleep.calls == [pytest.approx(2.0 * (0.5 + 0.5))]


def test_backoff_grows_and_caps() -> None:
    errs = [CliError("x", transient=True) for _ in range(4)]
    sleep = Sleeps()
    with pytest.raises(CliError):
        call_with_retry(
            FakeProvider(errs), REQ, max_attempts=4, cap_s=5.0, sleep=sleep, rand=lambda: 0.5
        )
    assert sleep.calls == [pytest.approx(2.0), pytest.approx(4.0), pytest.approx(5.0)]


def test_no_retry_non_transient() -> None:
    p = FakeProvider([CliError("bad", transient=False), FakeProvider.text("ok")])
    sleep = Sleeps()
    with pytest.raises(CliError) as ei:
        call_with_retry(p, REQ, sleep=sleep, rand=lambda: 0.5)
    assert ei.value.attempts == 1
    assert sleep.calls == []
    assert len(p.requests) == 1


def test_retry_exhaustion_reraises_with_attempts() -> None:
    p = FakeProvider([CliError(f"e{i}", transient=True) for i in range(3)])
    sleep = Sleeps()
    with pytest.raises(CliError, match="e2") as ei:
        call_with_retry(p, REQ, max_attempts=3, sleep=sleep, rand=lambda: 0.5)
    assert ei.value.attempts == 3
    assert len(sleep.calls) == 2


@pytest.mark.parametrize(
    "err",
    [
        UsageLimitError("limit", reset_hint="5pm"),
        AuthModeError("api key"),
        ToolUseDetected("Bash"),
        ServedModelMismatch("a", "b"),
        CliTimeout("slow"),
    ],
)
def test_never_retried(err: Exception) -> None:
    p = FakeProvider([err, FakeProvider.text("ok")])  # type: ignore[list-item]
    sleep = Sleeps()
    with pytest.raises(type(err)):
        call_with_retry(p, REQ, sleep=sleep, rand=lambda: 0.5)
    assert sleep.calls == []
    assert len(p.requests) == 1


def test_usage_limit_not_retried() -> None:
    p = FakeProvider([UsageLimitError("limit")])
    with pytest.raises(UsageLimitError):
        call_with_retry(p, REQ, sleep=Sleeps(), rand=lambda: 0.5)


def test_validate_called_up_front() -> None:
    bad = dataclasses.replace(REQ, timeout_s=0)
    p = FakeProvider([FakeProvider.text("ok")])
    with pytest.raises(Exception, match="timeout_s"):
        call_with_retry(p, bad, sleep=Sleeps())
    assert p.requests == []


def test_budget_blocks_before_exceeding() -> None:
    b = RunBudget(2, 1.0)
    b.check_can_start()
    b.charge(usd=0.1)
    b.check_can_start()
    b.charge(usd=0.1)
    assert (b.calls_used, b.usd_used) == (2, pytest.approx(0.2))
    with pytest.raises(BudgetExceeded):
        b.check_can_start()


def test_budget_notional_cap() -> None:
    b = RunBudget(100, 1.0)
    b.charge(usd=1.0)
    with pytest.raises(BudgetExceeded):
        b.check_can_start()


def test_lane_gate_probe_resume() -> None:
    now = [0.0]
    sleep = Sleeps()

    def adv(s: float) -> None:
        sleep(s)
        now[0] += s

    g = LaneGate(clock=lambda: now[0], sleep=adv)
    assert g.is_open()
    g.trip("5pm")
    assert not g.is_open()
    results = iter([False, False, True])
    g.wait_until_open(lambda: next(results))
    assert g.is_open()
    assert sleep.calls == [900.0, 900.0]


def test_lane_gate_timeout() -> None:
    now = [0.0]

    def adv(s: float) -> None:
        now[0] += s

    g = LaneGate(probe_interval_s=900.0, clock=lambda: now[0], sleep=adv)
    g.trip(None)
    with pytest.raises(LaneTimeout):
        g.wait_until_open(lambda: False, max_wait_s=3600)
    assert not g.is_open()


def _with_input(n: int, api: int | None = None, wall: int = 0):
    c = FakeProvider.text("ok", usage=Usage(input=n, output=1))
    return dataclasses.replace(c, latency_ms_wall=wall, latency_ms_api=api)


def test_calibrate_overhead_median() -> None:
    p = FakeProvider(
        [_with_input(600, 10, 100), _with_input(700, None, 300), _with_input(650, 30, 200)]
    )
    o = calibrate_overhead(p, "m", "low")
    assert o == Overhead(input_side_tokens=650, latency_ms_wall=200, latency_ms_api=10)
    r = p.requests[0]
    assert (r.system, r.prompt, r.max_output_tokens, r.timeout_s) == (
        "",
        "Reply with exactly: ok",
        None,
        120,
    )
    assert len(p.requests) == 3


def test_calibrate_overhead_all_api_none() -> None:
    p = FakeProvider([_with_input(1) for _ in range(3)])
    assert calibrate_overhead(p, "m", "low").latency_ms_api is None


def test_quota_pressure_claude_fixture() -> None:
    for line in (FIX / "claude" / "stream_init_haiku.jsonl").read_text().splitlines():
        ev = json.loads(line)
        if ev.get("type") == "rate_limit_event":
            info = ev["rate_limit_info"]
            assert quota_pressure(info) == pytest.approx(0.7)
            assert quota_pressure({"rate_limit_info": info}) == pytest.approx(0.7)
            return
    raise AssertionError("no rate_limit_event")


def test_quota_pressure_claude_rejected() -> None:
    assert quota_pressure({"status": "rejected", "unifiedWindows": {}}) == 1.0


def test_quota_pressure_codex() -> None:
    q = {
        "primary": {"used_percent": 0.0},
        "secondary": {"used_percent": 12.0},
    }
    assert quota_pressure(q) == pytest.approx(0.12)
    assert quota_pressure({"rate_limits": q}) == pytest.approx(0.12)
    assert quota_pressure({"primary": {"used_percent": 250.0}}) == 1.0


def test_quota_pressure_unknown() -> None:
    assert quota_pressure(None) is None
    assert quota_pressure({}) is None
    assert quota_pressure({"foo": 1}) is None


def test_threshold() -> None:
    assert QUOTA_PAUSE_THRESHOLD == 0.85
