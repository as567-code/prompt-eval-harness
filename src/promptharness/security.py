"""Security utilities: safe YAML, ID/path validation, env allowlist, log redaction."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
# Keys a caller may add through ``safe_env(extra=...)`` beyond the inherited allowlist.
_EXTRA_ALLOWLIST = frozenset({"CLAUDE_CODE_MAX_OUTPUT_TOKENS"})
_SECRET_KEY_RE = re.compile(
    r"(?i)(API[_-]?KEY|AUTH_TOKEN|SECRET|PASSWORD|CREDENTIAL|(?:^|_)TOKEN(?:_|$)"
    r"|_BASE_URL$|^CLAUDE_CODE_USE_|^(AWS|GOOGLE|AZURE|CLOUDSDK)_|^NODE_OPTIONS$)"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer ***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{3,}"), "sk-***"),
    (re.compile(r"\b(gh[pousr])_[A-Za-z0-9]{16,}"), r"\1_***"),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_]*(?:api[_-]?key|token|secret|password))"
            r"[\"']?\s*[=:]\s*"
            r"(?:\"(?:[^\"\\]|\\.)*\"\S*|'(?:[^'\\]|\\.)*'\S*|\S+)"
        ),
        r"\1=***",
    ),
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
    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        raise PathEscapeError(f"path {candidate} escapes root {resolved_root}")
    return candidate


def safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child-process env from an allowlist, never including secrets."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    if extra:
        permitted = set(_ENV_ALLOWLIST) | _EXTRA_ALLOWLIST
        env.update({k: v for k, v in extra.items() if k in permitted})
    return {k: v for k, v in env.items() if not _SECRET_KEY_RE.search(k)}


def _redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _redact_record(record: logging.LogRecord) -> None:
    """Redact message, exception text and stack info of ``record`` in place."""
    try:
        rendered = record.getMessage()
    except Exception:  # bad format args must never break the caller
        try:
            rendered = str(record.msg)
            if record.args:
                rendered += " " + repr(record.args)
        except Exception:  # str()/repr() of hostile objects can raise too
            rendered = "<unrenderable log message>"
    record.msg = _redact(rendered)
    record.args = None
    if record.exc_info:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.exc_info = None
    if record.exc_text:
        record.exc_text = _redact(record.exc_text)
    if record.stack_info:
        record.stack_info = _redact(record.stack_info)


class RedactingFilter(logging.Filter):
    """Logging filter that masks API keys, GitHub tokens and bearer tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def _install_record_factory() -> None:
    """Wrap the global LogRecord factory so every record is redacted at creation."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_peh_redacting", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        _redact_record(record)
        return record

    factory._peh_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Redact secrets in log records.

    With a ``logger``, attach a ``RedactingFilter`` to it (once). With no argument,
    wrap the global record factory (once) so records from every logger, including
    child loggers and third-party libraries, are redacted.
    """
    if logger is None:
        _install_record_factory()
    elif not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
