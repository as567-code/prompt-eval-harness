"""Token usage accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call.

    The buckets are disjoint: ``input`` EXCLUDES the ``cache_read`` and cache-write
    tokens, and ``reasoning`` is a subset of ``output``. Parsers must subtract cached
    tokens from providers that include them (Codex ``input_tokens`` includes
    ``cached_input_tokens``).
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0

    @property
    def input_side(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def net_of(self, overhead: int) -> Usage:
        """Subtract a stable-prefix overhead: cache_read, 1h write, 5m write, then input."""
        remaining = max(overhead, 0)
        updates: dict[str, int] = {}
        for name in ("cache_read", "cache_write_1h", "cache_write_5m", "input"):
            value: int = getattr(self, name)
            taken = min(value, remaining)
            remaining -= taken
            updates[name] = value - taken
        return replace(self, **updates)
