"""Structured diagnostics for ownership cards, only after their terminal frame is emitted."""
import threading

import pytest

from hermes_cli.active_sessions import ActiveSessionRefusal
from tui_gateway import server


@pytest.mark.parametrize("ledger,cause,pid,expected", [
    ("queued", None, None, "ledger_queued"),
    ("started", None, 123, "ledger_started"),
    ("unknown", None, None, "unknown"),
    (None, "lease_foreign", 123, "lease_foreign"),
    (None, "lease_self_orphan", 123, "lease_self_orphan"),
    (None, "update_marker", 123, "update_marker"),
    (None, "untrusted label", None, "unknown"),
])
def test_each_emitted_ownership_card_has_one_structured_warning(monkeypatch, caplog, ledger, cause, pid, expected):
    session = {"session_key": "owned-chat", "history_lock": threading.RLock(), "agent": None}
    error = ActiveSessionRefusal("held elsewhere", "SESSION_NOT_OWNED")
    error.ledger_status, error.ownership_cause, error.holder_pid = ledger, cause, pid
    frames = []
    monkeypatch.setattr(server, "_emit", lambda *a: frames.append(a))
    monkeypatch.setattr(server, "_fail_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "render_message", lambda *a: "")
    with caplog.at_level("WARNING"):
        server._emit_terminal_turn_error("live", session, error,
            error_surface={"layer": "gateway", "code": "SESSION_NOT_OWNED", "retryable": False}, retire_marker=False)
    assert len(frames) == 1 and frames[0][0] == "message.complete"
    warnings = [r.message for r in caplog.records if "session_ownership_card shown" in r.message]
    assert warnings == [f"session_ownership_card shown session=owned-chat cause={expected} holder_pid={pid or 0}"]


def test_nonownership_error_has_no_ownership_warning(monkeypatch, caplog):
    session = {"session_key": "owned-chat", "history_lock": threading.RLock(), "agent": None}
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_fail_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "render_message", lambda *a: "")
    server._emit_terminal_turn_error("live", session, "failed",
        error_surface={"layer": "gateway", "code": "SESSION_COORDINATION_UNAVAILABLE", "retryable": False},
        retire_marker=False)
    assert "session_ownership_card shown" not in caplog.text
