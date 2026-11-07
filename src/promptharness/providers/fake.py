"""Scripted provider for tests; never touches a CLI."""

from __future__ import annotations

from collections.abc import Sequence

from promptharness.providers.base import (
    Completion,
    CompletionRequest,
    ProviderError,
    Status,
)
from promptharness.usage import Usage


class FakeProvider:
    def __init__(self, script: Sequence[Completion | ProviderError], name: str = "fake") -> None:
        self.name = name
        self._script = list(script)
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> Completion:
        self.requests.append(req)
        if not self._script:
            raise IndexError("FakeProvider script exhausted")
        item = self._script.pop(0)
        if isinstance(item, ProviderError):
            raise item
        return item

    @classmethod
    def text(cls, text: str, model: str = "fake-model", usage: Usage | None = None) -> Completion:
        return Completion(
            text=text,
            structured=None,
            status=Status.OK,
            stop_reason="end_turn",
            served_model=model,
            usage_gross=usage if usage is not None else Usage(input=10, output=10),
            latency_ms_api=None,
            latency_ms_wall=0,
            cli_attempts=1,
            tool_attempts=0,
            output_cap_applied=False,
            cli_version="fake",
            provider="fake",
            raw=text,
        )
