"""Real prompt dispatch refusal must not clear a replacement turn."""
from contextlib import nullcontext
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import backend_retirement
from tui_gateway import server, turn_admission as ta

pytestmark = pytest.mark.platforms("posix")


def test_prompt_worker_refusal_preserves_replacement_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: True)
    # Fork: upstream's _admit_prompt_turn takes a keyword-only report_ownership_refusal.
    monkeypatch.setattr(server, "_admit_prompt_turn", lambda *a, **kw: ([], SimpleNamespace()))
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda *a: nullcontext())
    monkeypatch.setattr(server, "_routing_provenance_db", lambda *a: nullcontext(None))
    monkeypatch.setattr(server, "_sessions", {})
    def replace_on_start(*args):
        with session["history_lock"]:
            ta.settle(session)
            session["running"] = False
        with ta.admission(session) as accepted:
            assert accepted
            session["running"] = True
            session["inflight_turn"] = {"user": "replacement"}
    # Interleave Stop/resubmit after admission but before the actual worker start.
    monkeypatch.setattr(server, "_emit", replace_on_start)
    try:
        with ta.stage(session):
            assert server._run_admitted_prompt("r", "s", session, "old") is False
            assert session["running"]
            assert session["inflight_turn"] == {"user": "replacement"}
        assert fence.active_count() == 1
    finally:
        ta.settle(session)
    assert fence.active_count() == 0


@pytest.mark.parametrize("new_claim", [False, True])
def test_nested_submit_inherits_waiter_but_explicit_followup_adopts(tmp_path, monkeypatch, new_claim):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
    seen = []
    monkeypatch.setattr(server, "_run_admitted_prompt", lambda *a, **kw: seen.append(ta.owns(session)))
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    try:
        with ta.stage(session):
            ta.settle(session)
            session["running"] = False
            with ta.admission(session) as accepted:
                assert accepted
                session["running"] = True
            if new_claim:
                server._dispatch_followup_turn("r", "s", session, "new", "test")
            else:
                server._run_prompt_submit("r", "s", session, "old")
        assert seen == [new_claim]
        assert session["running"]
        assert fence.active_count() == 1
    finally:
        ta.settle(session)
    assert fence.active_count() == 0


def test_notification_claim_from_stale_worker_context_adopts_new_turn(tmp_path, monkeypatch):
    """A completion drained after a settled turn (worker context still holds T1)
    must adopt its own freshly claimed T2 rather than inherit the stale T1."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
    seen = []
    monkeypatch.setattr(server, "_run_admitted_prompt", lambda *a, **kw: seen.append(ta.owns(session)) or True)
    try:
        with ta.stage(session):
            ta.settle(session)
            session["running"] = False
            assert server._notif_claim_turn(session)
            assert server._notif_run_submit("r", "s", session, "drained")
        assert seen == [True]
    finally:
        session["running"] = False
        ta.settle(session)
    assert fence.active_count() == 0
