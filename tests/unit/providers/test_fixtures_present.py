import getpass
import json
import re
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "cli"
CLAUDE_JSON = ["ok_haiku", "schema_haiku", "toolbait_haiku", "cap_haiku"]
CODEX_JSONL = ["ok_terra", "schema_terra", "toolbait_terra", "cap_terra"]
ALL_FILES = sorted(p for p in FIX.rglob("*") if p.is_file())
EMAIL_LIKE = re.compile(r"@[A-Za-z0-9-]+\.[A-Za-z]{2,}")


def _jsonl(path: Path) -> list[object]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"{path} is empty"
    return [json.loads(ln) for ln in lines]


@pytest.mark.parametrize("name", CLAUDE_JSON)
def test_claude_fixture_is_one_json_object(name: str) -> None:
    data = json.loads((FIX / "claude" / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["type"] == "result"


def test_claude_stream_init_fixture() -> None:
    events = _jsonl(FIX / "claude" / "stream_init_haiku.jsonl")
    init = events[0]
    assert isinstance(init, dict)
    assert init["type"] == "system" and init["subtype"] == "init"
    assert init["apiKeySource"] == "none"


@pytest.mark.parametrize("name", CODEX_JSONL)
def test_codex_fixtures_parse_and_share_thread_id(name: str) -> None:
    stdout = _jsonl(FIX / "codex" / f"{name}.jsonl")
    rollout = _jsonl(FIX / "codex" / "rollouts" / f"{name}.jsonl")
    first = stdout[0]
    assert isinstance(first, dict) and first["type"] == "thread.started"
    meta = rollout[0]
    assert isinstance(meta, dict) and meta["type"] == "session_meta"
    assert meta["payload"]["id"] == first["thread_id"]


def test_codex_login_status_fixture() -> None:
    text = (FIX / "codex" / "login_status.txt").read_text(encoding="utf-8")
    assert "ChatGPT" in text


def test_fixture_count() -> None:
    assert len(ALL_FILES) == 4 + 1 + 4 + 4 + 1


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(FIX)))
def test_fixtures_present(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    low = text.lower()
    assert "/Users/" not in text
    assert "credential" not in low
    assert "account_id" not in low
    assert "user_id" not in low
    assert getpass.getuser() not in text
    assert not EMAIL_LIKE.search(text)


TZ_LIKE = re.compile(r"\b(?:America|Europe|Asia|Africa|Australia|Pacific|Atlantic)/[A-Z][A-Za-z_]+")


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(FIX)))
def test_fixtures_have_no_tz_or_temp_hash(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not TZ_LIKE.search(text)
    assert "/var/folders/" not in text
    assert "-var-folders-" not in text


@pytest.mark.parametrize("name", CODEX_JSONL)
def test_rollout_message_items_are_redacted(name: str) -> None:
    for line in _jsonl(FIX / "codex" / "rollouts" / f"{name}.jsonl"):
        assert isinstance(line, dict)
        payload = line.get("payload")
        if line["type"] == "response_item" and payload["type"] == "message":
            assert payload["role"]
            for part in payload["content"]:
                assert part["text"] == "REDACTED_CONTEXT"
        if line["type"] == "world_state":
            assert "host_skills" not in json.dumps(line) or "REDACTED_CONTEXT" in json.dumps(line)
