"""A non-retryable LOCAL exception (no HTTP status) logs its traceback; an HTTP 4xx stays one line.

Chat 20261110_184601_52504d failed every turn for hours on a TypeError raised while assembling the
request, and agent.log/errors.log held only the message, so the raising frame had to be rebuilt
offline. The traceback rides on the terminal "Non-retryable client error" record.
"""
import logging

import pytest

from agent.error_classifier import classify_api_error
from agent.turn_recovery import nonretryable_client_error_result
from tests.agent.test_failed_turn_chat_copy import _Agent, _Http


def _run(error, status):
    classified = classify_api_error(error, provider="anthropic", model="claude-opus-5-5")
    return nonretryable_client_error_result(
        _Agent(), error, classified, status_code=status, api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5-5",
    )


def _records(caplog):
    return [r for r in caplog.records if "Non-retryable client error" in r.getMessage()]


def test_local_type_error_logs_traceback(caplog):
    def _scan(value):
        return value.get("type") in {"image"}  # the real failing expression's shape
    with pytest.raises(TypeError) as raised:
        _scan({"type": {"type": "string"}})
    error = raised.value
    with caplog.at_level(logging.ERROR, logger="agent.conversation_loop"):
        _run(error, None)
    (record,) = _records(caplog)
    assert record.exc_info and record.exc_info[1] is error
    assert "_scan" in caplog.text and "Traceback" in caplog.text


def test_http_client_error_stays_one_line(caplog):
    with caplog.at_level(logging.ERROR, logger="agent.conversation_loop"):
        _run(_Http(404, "HTTP 404: model not found"), 404)
    (record,) = _records(caplog)
    assert not record.exc_info
