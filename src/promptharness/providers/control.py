"""Control layer: retry, run budget, lane gate, overhead calibration, quota pressure."""

from __future__ import annotations

import dataclasses
import random
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from promptharness.providers.base import (
    CliError,
    Completion,
    CompletionRequest,
    Effort,
    Provider,
)

QUOTA_PAUSE_THRESHOLD = 0.85


class BudgetExceeded(Exception):
    """The run's call or notional-spend cap is reached (a harness decision)."""


class LaneTimeout(Exception):
    """A paused lane did not reopen within the allowed wait (a harness decision)."""


def call_with_retry(
    provider: Provider,
    req: CompletionRequest,
    *,
    max_attempts: int = 4,
    base_s: float = 2.0,
    cap_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> Completion:
    """Retry transient CliError with jittered exponential backoff; nothing else is retried."""
    req.validate()
    n = 0
    while True:
        n += 1
        try:
            completion = provider.complete(req)
        except CliError as err:
            if not err.transient or n >= max_attempts:
                err.attempts = n
                raise
            sleep(min(cap_s, base_s * 2 ** (n - 1)) * (0.5 + rand()))
            continue
        return dataclasses.replace(completion, cli_attempts=n)


class RunBudget:
    def __init__(self, max_calls: int, max_notional_usd: float) -> None:
        self.max_calls = max_calls
        self.max_notional_usd = max_notional_usd
        self.calls_used = 0
        self.usd_used = 0.0

    def check_can_start(self) -> None:
        if self.calls_used >= self.max_calls:
            raise BudgetExceeded(f"call cap reached: {self.calls_used}/{self.max_calls}")
        if self.usd_used >= self.max_notional_usd:
            raise BudgetExceeded(
                f"notional spend cap reached: ${self.usd_used:.4f}/${self.max_notional_usd:.4f}"
            )

    def charge(self, *, calls: int = 1, usd: float) -> None:
        self.calls_used += calls
        self.usd_used += usd


class LaneGate:
    def __init__(
        self,
        probe_interval_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.probe_interval_s = probe_interval_s
        self._clock = clock
        self._sleep = sleep
        self._open = True
        self.reset_hint: str | None = None

    def trip(self, reset_hint: str | None) -> None:
        self._open = False
        self.reset_hint = reset_hint

    def is_open(self) -> bool:
        return self._open

    def wait_until_open(self, probe: Callable[[], bool], *, max_wait_s: float = 6 * 3600) -> None:
        deadline = self._clock() + max_wait_s
        while True:
            if probe():
                self._open = True
                self.reset_hint = None
                return
            if self._clock() + self.probe_interval_s > deadline:
                raise LaneTimeout(f"lane still closed after {max_wait_s}s")
            self._sleep(self.probe_interval_s)


@dataclass(frozen=True)
class Overhead:
    input_side_tokens: int
    latency_ms_wall: int
    latency_ms_api: int | None


def calibrate_overhead(provider: Provider, model: str, effort: Effort, *, n: int = 3) -> Overhead:
    """Measure the CLI's fixed prompt overhead as the per-field median of n trivial calls."""
    req = CompletionRequest(
        model=model,
        effort=effort,
        system="",
        prompt="Reply with exactly: ok",
        max_output_tokens=None,
        timeout_s=120,
    )
    samples = [provider.complete(req) for _ in range(n)]
    api = [c.latency_ms_api for c in samples if c.latency_ms_api is not None]
    return Overhead(
        input_side_tokens=statistics.median_low(c.usage_gross.input_side for c in samples),
        latency_ms_wall=statistics.median_low(c.latency_ms_wall for c in samples),
        latency_ms_api=statistics.median_low(api) if api else None,
    )


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    return float(x)


def quota_pressure(quota: Mapping[str, object] | None) -> float | None:
    """Max utilization in [0, 1] across all quota windows, or None if the shape is unknown."""
    if not isinstance(quota, Mapping):
        return None
    for wrapper in ("rate_limit_info", "rate_limits"):
        inner = quota.get(wrapper)
        if isinstance(inner, Mapping):
            quota = inner
            break
    found: list[float] = []
    if "unifiedWindows" in quota or "status" in quota:
        if quota.get("status") == "rejected":
            found.append(1.0)
        windows = quota.get("unifiedWindows")
        if isinstance(windows, Mapping):
            for key in ("five_hour", "seven_day"):
                w = windows.get(key)
                u = _num(w.get("utilization")) if isinstance(w, Mapping) else None
                if u is not None:
                    found.append(u)
    for key in ("primary", "secondary"):
        w = quota.get(key)
        pct = _num(w.get("used_percent")) if isinstance(w, Mapping) else None
        if pct is not None:
            found.append(pct / 100.0)
    if not found:
        return None
    return min(1.0, max(0.0, max(found)))
