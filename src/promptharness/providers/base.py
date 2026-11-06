"""Provider protocol, request/completion types, and the error taxonomy."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, get_args

from promptharness.usage import Usage

Effort = Literal["low", "medium", "high", "xhigh", "max"]

MAX_PROMPT_BYTES = 200_000
MAX_RAW_BYTES = 1_000_000


class ProviderError(Exception):
    """Base class for every provider failure."""

    error_class: str = "cli_error"

    def __init__(self, message: str = "", *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class InvalidRequestError(ProviderError):
    error_class = "cli_error"


class CliError(ProviderError):
    error_class = "cli_error"

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class CliTimeout(ProviderError):
    error_class = "timeout"


class UsageLimitError(ProviderError):
    error_class = "usage_limit"

    def __init__(self, message: str, *, reset_hint: str | None = None) -> None:
        super().__init__(message)
        self.reset_hint = reset_hint


class ServedModelMismatch(ProviderError):
    error_class = "served_model_mismatch"

    def __init__(self, requested: str, served: str) -> None:
        super().__init__(f"requested model {requested!r} but CLI served {served!r}")
        self.requested = requested
        self.served = served


class ToolUseDetected(ProviderError):
    error_class = "tool_use_detected"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Status(enum.StrEnum):
    OK = "ok"
    TRUNCATED = "truncated"
    REFUSAL = "refusal"


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    effort: Effort
    system: str
    prompt: str
    json_schema: Mapping[str, object] | None = None
    max_output_tokens: int | None = None
    timeout_s: float = 300.0

    def validate(self) -> None:
        if "\x00" in self.system or "\x00" in self.prompt:
            raise InvalidRequestError("NUL byte in system or prompt")
        if len(self.prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise InvalidRequestError(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
        if self.effort not in get_args(Effort):
            raise InvalidRequestError(f"unknown effort {self.effort!r}")
        if self.timeout_s <= 0:
            raise InvalidRequestError("timeout_s must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise InvalidRequestError("max_output_tokens must be >= 1")


@dataclass(frozen=True)
class Completion:
    text: str
    structured: Mapping[str, object] | None
    status: Status
    stop_reason: str | None
    served_model: str
    usage_gross: Usage
    latency_ms_api: int | None
    latency_ms_wall: int
    cli_attempts: int
    tool_attempts: int
    output_cap_applied: bool
    cli_version: str
    provider: str
    raw: str  # callers cap at MAX_RAW_BYTES (1 MB)


class Provider(Protocol):
    name: str

    def complete(self, req: CompletionRequest) -> Completion: ...
