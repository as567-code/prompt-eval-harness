import logging
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
            "and Bearer eyJabc"
        )
        logger.info("arg %s", "sk-secret-abc")
    first, second = (r.getMessage() for r in caplog.records)
    for secret in ("AbC_123xyz", "Zz9", "abcdefghijklmnopqrstuv", "eyJabc"):
        assert secret not in first
    assert "sk-***" in first
    assert "ghp_***" in first
    assert "Bearer ***" in first
    assert second == "arg sk-***"


def test_install_redaction_idempotent_and_default() -> None:
    logger = logging.getLogger("peh.redact.idem")
    install_redaction(logger)
    install_redaction(logger)
    assert sum(isinstance(f, RedactingFilter) for f in logger.filters) == 1
    install_redaction()
    root = logging.getLogger()
    assert any(isinstance(f, RedactingFilter) for f in root.filters)
    for f in [f for f in root.filters if isinstance(f, RedactingFilter)]:
        root.removeFilter(f)
