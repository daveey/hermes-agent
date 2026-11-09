"""Standalone sender failures preserve delivery outcomes without leaking raw exception traces."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.telegram import standalone
from tests.tools.test_telegram_send_message_caption import _install_telegram_mock, _make_bot, _no_proxy


@pytest.mark.parametrize("boundary", ["caption", "media", "outer"])
def test_sender_failure_boundaries_sanitize_logs_and_results(monkeypatch, tmp_path, caplog, boundary):
    bot = _make_bot()
    _no_proxy(monkeypatch)
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))
    secret = "private-canary-value"
    error = RuntimeError(f"request failed access_token={secret}")
    path = tmp_path / "file.pdf"
    message = "caption"
    if boundary == "caption":
        bot.send_message = AsyncMock(side_effect=error)
    elif boundary == "media":
        path.write_bytes(b"test")
        bot.send_document = AsyncMock(side_effect=error)
        message = ""
    else:
        monkeypatch.setattr(standalone, "_telegram_format", MagicMock(side_effect=error))
    caplog.set_level(logging.WARNING, logger=standalone.__name__)
    result = asyncio.run(standalone._send_telegram("tok", "123", message, media_files=[(str(path), False)]))
    assert result.get("error"), result
    assert secret not in str(result)
    assert "access_token=***" in caplog.text
    assert secret not in caplog.text
    errors = [record for record in caplog.records if "access_token=" in record.getMessage()]
    assert errors
    assert all(record.exc_info is None for record in errors)


def test_missing_media_delivers_caption_fallback(monkeypatch, tmp_path):
    bot = _make_bot()
    _no_proxy(monkeypatch)
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))
    result = asyncio.run(standalone._send_telegram(
        "tok", "123", "caption", media_files=[(str(tmp_path / "missing.pdf"), False)]))
    assert result.get("success"), result
    assert bot.send_message.await_count == 1
    assert any("Media file not found" in warning for warning in result["warnings"])
