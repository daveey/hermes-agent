"""~/.config/token-broker holds the enrolled token-broker client token.

With it, anyone can ``token-broker exec`` auto-tier scopes (e.g. a tailnet SSH key)
with no approval, so it is the same credential class as ``~/.ssh``: write-denied,
and — unlike the other HOME_CREDENTIAL_DIRS — read-denied too, because nothing an
agent does needs the raw token (the ``token-broker`` CLI reads it itself).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent.file_safety as fs


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(fs, "_guard_homes", lambda path="": {str(home)})
    return home


def _touch(base: Path, rel: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dummy", encoding="utf-8")
    return p


@pytest.mark.parametrize("rel", ["token", "token.expired-20261022T050000Z", "nested/anything"])
def test_token_broker_files_are_read_denied(home, rel):
    path = _touch(home, f".config/token-broker/{rel}")
    for candidate in (str(path), f"~/.config/token-broker/{rel}"):
        err = fs.get_read_block_error(candidate)
        assert err and "token-broker" in err, candidate
        with pytest.raises(ValueError):
            fs.raise_if_read_blocked(candidate)


def test_token_broker_dir_itself_is_read_denied(home):
    (home / ".config" / "token-broker").mkdir(parents=True)
    assert fs.get_read_block_error(str(home / ".config" / "token-broker"))


def test_symlink_into_token_broker_is_read_denied(home, tmp_path):
    target = _touch(home, ".config/token-broker/token")
    link = tmp_path / "innocent.txt"
    os.symlink(target, link)
    assert fs.get_read_block_error(str(link))


def test_token_broker_is_write_denied(home):
    path = _touch(home, ".config/token-broker/token")
    assert fs.is_write_denied(str(path))
    assert ".config/token-broker" in fs.HOME_CREDENTIAL_DIRS


def test_neighbours_stay_readable(home):
    for rel in (".config/token-broker-notes.txt", ".config/other/token", "token-broker/token"):
        assert fs.get_read_block_error(str(_touch(home, rel))) is None, rel


@pytest.mark.asyncio
async def test_vision_analyze_refuses_token_broker_token(home):
    import json
    from unittest.mock import AsyncMock, patch

    from tools.vision_tools import vision_analyze_tool

    path = _touch(home, ".config/token-broker/token")
    with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock) as mock_llm:
        result = json.loads(await vision_analyze_tool(str(path), "extract text"))
    assert result["success"] is False
    assert "token-broker" in result["error"]
    mock_llm.assert_not_awaited()


def test_read_file_tool_refuses_token_broker_token(home):
    import json

    from tools.file_tools import read_file_tool

    path = _touch(home, ".config/token-broker/token")
    result = json.loads(read_file_tool(str(path)))
    assert "token-broker" in (result.get("error") or ""), result
    assert "dummy" not in json.dumps(result)
