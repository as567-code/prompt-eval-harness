"""Security utilities: safe YAML, ID/path validation, env allowlist, log redaction."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "__CF_USER_TEXT_ENCODING",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)
_SECRET_KEY_RE = re.compile(r"(?i)(API_KEY|AUTH_TOKEN|SECRET|_TOKEN$|PASSWORD)")

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+\S+"), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{3,}"), "sk-***"),
    (re.compile(r"\b(gh[pousr])_[A-Za-z0-9]{16,}"), r"\1_***"),
)


class ConfigError(Exception):
    """Raised when a configuration file is too large or cannot be parsed."""


class PathEscapeError(ValueError):
    """Raised when a joined path resolves outside its root."""


def load_yaml(path: Path, *, max_bytes: int = 1_000_000) -> object:
    """Load a YAML file with ``yaml.safe_load``, enforcing a size cap."""
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ConfigError(f"{path}: {size} bytes exceeds limit of {max_bytes}")
        with path.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    if len(data) > max_bytes:
        raise ConfigError(f"{path}: exceeds limit of {max_bytes} bytes")
    try:
        return yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def validate_id(value: str, kind: str) -> str:
    """Return ``value`` if it is a valid identifier, else raise ``ValueError``."""
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {kind} id {value!r}: must match {_ID_RE.pattern}")
    return value


def safe_join(root: Path, *parts: str) -> Path:
    """Join ``parts`` under ``root``, rejecting lexical or symlink escapes."""
    resolved_root = root.resolve()
    candidate = Path(resolved_root, *parts).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise PathEscapeError(f"path {candidate} escapes root {resolved_root}")
    return candidate


def safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child-process env from an allowlist, never including secrets."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    if extra:
        env.update(extra)
    return {k: v for k, v in env.items() if not _SECRET_KEY_RE.search(k)}


def _redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that masks API keys, GitHub tokens and bearer tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = None
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Attach a ``RedactingFilter`` to ``logger`` (root by default), once."""
    target = logger if logger is not None else logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())
