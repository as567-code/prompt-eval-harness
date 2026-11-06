import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from promptharness.security import (
    ConfigError,
    PathEscapeError,
    RedactingFilter,
    install_redaction,
    load_yaml,
    safe_env,
    safe_join,
    validate_id,
)


def test_load_yaml_ok(tmp_path: Path) -> None:
    f = tmp_path / "a.yaml"
    f.write_text("a: 1\n")
    assert load_yaml(f) == {"a": 1}


def test_load_yaml_rejects_python_tag(tmp_path: Path) -> None:
    f = tmp_path / "a.yaml"
    f.write_text('!!python/object/apply:os.system ["echo x"]\n')
    with pytest.raises(ConfigError):
        load_yaml(f)


def test_load_yaml_rejects_oversize(tmp_path: Path) -> None:
    f = tmp_path / "a.yaml"
    f.write_bytes(b"#" * 11)
    with pytest.raises(ConfigError):
        load_yaml(f, max_bytes=10)


def test_load_yaml_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_yaml(tmp_path / "nope.yaml")


@pytest.mark.parametrize("bad", ["../x", "A", "a/b", "ab", "x" * 65])
def test_validate_id_rejects_traversal(bad: str) -> None:
    with pytest.raises(ValueError, match="case"):
        validate_id(bad, "case")


def test_validate_id_accepts_good() -> None:
    assert validate_id("abc-123", "case") == "abc-123"


def test_safe_join_ok(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a", "b") == tmp_path.resolve() / "a" / "b"


def test_safe_join_blocks_escape(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        safe_join(tmp_path, "..", "etc")


def test_safe_join_blocks_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(PathEscapeError):
        safe_join(tmp_path, "link", "passwd")


def test_safe_env_strips_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(k, "v")
    monkeypatch.setenv("HOME", "/h")
    env = safe_env()
    assert env["HOME"] == "/h"
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN"):
        assert k not in env
    env = safe_env({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64", "MY_SECRET": "x"})
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64"
    assert "MY_SECRET" not in env


def test_redaction(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("peh.redact.test")
    install_redaction(logger)
    with caplog.at_level(logging.INFO, logger="peh.redact.test"):
        logger.info(
            "key sk-ant-api03-AbC_123xyz and sk-proj-Zz9 and ghp_abcdefghijklmnopqrstuv "
            "and Bearer eyJabc and bearer abc.def and github_pat_11ABCDEFG0123456789_abcdef "
            "and api_key=hunter2"
        )
        logger.info("arg %s", "sk-secret-abc")
    first, second = (r.getMessage() for r in caplog.records)
    for secret in (
        "AbC_123xyz",
        "Zz9",
        "abcdefghijklmnopqrstuv",
        "eyJabc",
        "abc.def",
        "11ABCDEFG",
        "hunter2",
    ):
        assert secret not in first
    assert "github_pat_***" in first
    assert "api_key=***" in first
    assert "sk-***" in first
    assert "ghp_***" in first
    assert "Bearer ***" in first
    assert second == "arg sk-***"


def test_install_redaction_idempotent_per_logger() -> None:
    logger = logging.getLogger("peh.redact.idem")
    install_redaction(logger)
    install_redaction(logger)
    assert sum(isinstance(f, RedactingFilter) for f in logger.filters) == 1


@pytest.fixture
def restore_factory() -> Iterator[None]:
    original = logging.getLogRecordFactory()
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


def test_redaction_covers_child_loggers(
    restore_factory: None, caplog: pytest.LogCaptureFixture
) -> None:
    install_redaction()
    wrapped = logging.getLogRecordFactory()
    install_redaction()
    assert logging.getLogRecordFactory() is wrapped  # idempotent
    with caplog.at_level(logging.INFO):
        logging.getLogger("some.child.lib").info(
            "tok ghp_abcdefghijklmnopqrstuv key sk-ant-api03-AbC_123xyz %s", "Bearer eyJabc"
        )
        logging.getLogger("some.child.lib").info("task-force ready")
    msgs = [r.getMessage() for r in caplog.records]
    for secret in ("abcdefghijklmnopqrstuv", "AbC_123xyz", "eyJabc"):
        assert secret not in msgs[0]
    assert "ghp_***" in msgs[0]
    assert "sk-***" in msgs[0]
    assert "Bearer ***" in msgs[0]
    assert msgs[1] == "task-force ready"


def test_redaction_covers_exception_text(restore_factory: None) -> None:
    install_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("peh.redact.exc")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise ValueError("token sk-ant-api03-SECRETxyz")
        except ValueError:
            logger.exception("boom")
        logger.info("stack", stack_info=True)
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "boom" in out
    assert "ValueError" in out
    assert "SECRETxyz" not in out
