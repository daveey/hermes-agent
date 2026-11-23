"""TUI backend retirement includes detached children of idle parents."""
from __future__ import annotations

import signal

from tui_gateway import server
from tools import async_delegation as ad


def test_shutdown_marks_delegations_before_flush(monkeypatch):
    calls = []
    monkeypatch.setattr(ad, "interrupt_all", lambda **kw: calls.append(kw))
    monkeypatch.setattr(server, "_flush_sessions_before_exit", lambda: calls.append("flush"))
    monkeypatch.setattr(server, "_release_gateway_wake_owner", lambda: None)
    monkeypatch.setattr(server, "_stop_turns_before_exit", lambda: None)
    monkeypatch.setattr(server, "_journal_background_jobs_before_exit", lambda: None)
    monkeypatch.setattr(server, "_sessions", {})
    server._shutdown_sessions()
    assert calls == [{"reason": "gateway_shutdown", "resumable": True, "by_shutdown": True}, "flush"]


def test_sigterm_marks_delegations_before_flush(monkeypatch):
    calls = []
    monkeypatch.setattr(ad, "interrupt_all", lambda **kw: calls.append("children"))
    monkeypatch.setattr(server, "_flush_sessions_before_exit", lambda: calls.append("flush"))
    monkeypatch.setattr(server, "_stop_turns_before_exit", lambda: None)
    monkeypatch.setattr(server, "_journal_background_jobs_before_exit", lambda: None)
    monkeypatch.setattr(server, "_exit_flush_prev_handlers", {signal.SIGTERM: lambda *a: calls.append("previous")})
    server._handle_exit_flush_signal(signal.SIGTERM, None)
    assert calls == ["children", "flush", "previous"]


def test_shutdown_session_keeps_successor_debt(monkeypatch):
    from tools import delegate_tool_auto_resume as auto
    record = {"status": "running", "delegation_id": "deleg-x"}
    monkeypatch.setattr(ad, "_session_records", lambda *a: [record])
    monkeypatch.setattr(ad, "_interrupt_records", lambda *a: 1)
    superseded = []
    monkeypatch.setattr(auto, "supersede_for_session", lambda **kw: superseded.append(kw))
    assert ad.interrupt_for_session(session_key="chat", reason="tui_shutdown") == 1
    assert record.get("_interrupted_by") == "tui_shutdown"
    assert not superseded
    # Explicit person Stop still cancels the resume obligation.
    ad.interrupt_for_session(session_key="chat", reason="stop")
    assert superseded
