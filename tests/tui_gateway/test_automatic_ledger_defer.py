"""Gateway queue contention is a silent automatic retry, not a takeover request."""
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server


@pytest.fixture
def admission(monkeypatch):
    state = SimpleNamespace(refusal=None, errors=[], starts=[], now=100.0)
    session = {"session_key": "ledger-busy", "history_lock": threading.RLock(),
               "history": [], "running": True, "agent": SimpleNamespace(clear_interrupt=lambda: None),
               "attached_images": []}
    monkeypatch.setattr(server, "_session_moved_refusal", lambda _: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *args: state.refusal)
    monkeypatch.setattr(server, "_emit_terminal_turn_error", lambda *a, **k: state.errors.append(k))
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: state.starts.append(a))
    monkeypatch.setattr(server, "admit_synthesized_turn", lambda _: True)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: state.now))
    state.session = session
    return state


def _admit(state):
    state.session["running"] = True
    return server._admit_prompt_turn("live", state.session, "notice", None, None, None, None)


@pytest.mark.parametrize("status", ["queued", "started", "registered", "running"])
def test_automatic_ledger_busy_is_silent(admission, status, caplog):
    admission.session["_synthesized_turn"] = True
    admission.refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ledger_status=status)
    with caplog.at_level("INFO"):
        assert _admit(admission) is None
    assert admission.session["_turn_owner_refusal"] == "SESSION_NOT_OWNED"
    assert admission.errors == []
    assert admission.starts == []
    assert f"gateway queue holds {status} work" in caplog.text


@pytest.mark.parametrize("reason", ["SESSION_NOT_OWNED", "SESSION_COORDINATION_UNAVAILABLE"])
@pytest.mark.parametrize("status", ["unknown", None])
@pytest.mark.parametrize("synthesized", [True, False])
def test_actionable_owner_refusal_still_surfaces(admission, status, synthesized, reason):
    admission.session["_synthesized_turn"] = synthesized
    admission.refusal = SimpleNamespace(reason=reason)
    if status is not None:
        admission.refusal.ledger_status = status
    assert _admit(admission) is None
    assert len(admission.errors) == 1
    assert admission.errors[0]["error_surface"]["code"] == reason


@pytest.mark.parametrize("status", ["queued", "started", "registered", "running"])
def test_ledger_retry_stays_at_30_seconds_and_runs_when_clear(admission, status, monkeypatch):
    session = admission.session
    admission.refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ledger_status=status)
    turns = []

    def submit(rid, sid, record, text, **kwargs):
        admitted = _admit(admission)
        if admitted is None:
            return False
        turns.append(text)
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    for _ in range(6):
        assert server._notif_run_submit("rid", "live", session, "notice") is False
        assert session["_notif_owner_refused_until"] == admission.now + 30
        assert session["_notif_owner_refusal_delay"] == 30
        assert server._notif_owner_refused(session)
        admission.now += 30
        assert not server._notif_owner_refused(session)
    assert turns == [] and admission.errors == []
    admission.refusal = None
    assert server._notif_run_submit("rid", "live", session, "notice") is True
    assert turns == ["notice"]
    assert not server._notif_owner_refused(session)
    assert "_notif_owner_refusal_delay" not in session
    assert "_turn_owner_refusal" not in session


@pytest.mark.parametrize("status", ["unknown", None])
def test_actionable_refusal_keeps_exponential_backoff(admission, status):
    admission.refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ledger_status=status)
    admission.session["_notif_owner_refusal_delay"] = 60
    assert not server._notif_owner_preflight("live", admission.session)
    assert admission.session["_notif_owner_refused_until"] == admission.now + 60
    assert admission.session["_notif_owner_refusal_delay"] == 120


@pytest.mark.parametrize("gateway_owner", [True, False, None])
@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("reason,cause", [
    ("SESSION_NOT_OWNED", "lease_foreign"),
    ("SESSION_COORDINATION_UNAVAILABLE", "lease_foreign"),
    ("SESSION_NOT_OWNED", "lease_self_orphan"),
])
def test_durable_gateway_notification_refusal_is_not_a_takeover(
    admission, monkeypatch, gateway_owner, typed, reason, cause
):
    """Real notification submit/admission, not a mock of the second ownership gate."""
    from tui_gateway import gateway_background_holder as holders
    from hermes_cli import active_sessions
    import hermes_state_pidns
    import os

    pid = os.getpid() + 100000
    admission.refusal = SimpleNamespace(
        reason=reason, ownership_cause=cause, holder_pid=pid)
    owner = {"holder": f"pid={pid}:pidns=fixture:platform=tui"}
    monkeypatch.setattr(server, "_foreign_turn_owner", lambda _: owner)
    monkeypatch.setattr(active_sessions, "active_session_registry_snapshot", lambda _: [])
    monkeypatch.setattr(hermes_state_pidns, "holder_pid_checkable", lambda _: True)
    monkeypatch.setattr(holders, "dialed_gateway", lambda _: (
        None if gateway_owner is None else
        holders.DialedGateway(pid if gateway_owner else pid + 1, object())))
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: True)
    before_history = list(admission.session["history"])
    admission.session["_turn_marker_key"] = "original-live-marker"

    if typed:
        assert _admit(admission) is None
    else:
        assert server._notif_run_submit("rid", "live", admission.session, "notice") is False
        assert admission.session["_notif_owner_refused_until"] == admission.now + 30
        assert admission.session["_notif_owner_refusal_delay"] == 60
        assert "_synthesized_turn" not in admission.session
        # Repeated polls defer before attempting another submit during the backoff.
        assert server._notif_owner_refused(admission.session)

    deferred = not typed and gateway_owner is True and reason == "SESSION_NOT_OWNED" and cause == "lease_foreign"
    assert len(admission.errors) == int(not deferred)
    assert admission.starts == []
    assert admission.session["history"] == before_history
    assert admission.session["running"] is False
    assert admission.session["_turn_marker_key"] == "original-live-marker"


def test_typed_ledger_refusal_is_not_silenced(admission):
    admission.refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ledger_status="queued")
    assert _admit(admission) is None
    assert len(admission.errors) == 1
