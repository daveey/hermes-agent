"""A concluding TUI turn must not retire or discount its reused agent's next turn."""
import contextlib
import contextvars
import logging
import threading
import time
from types import SimpleNamespace

import pytest

import hermes_state_outcome as so
from tests.hermes_state.test_outcome_late_turn_end_refresh import _next_turn_starts, _tui_scope
from tests.hermes_state import test_outcome_turn_end_declaration as helpers
from tui_gateway import prompt_turn, server, turn_admission, turn_outcome
from tui_gateway.method_ctx import rebind
from tui_gateway.turn_marker import clear_turn_marker, read_turn_marker, record_turn_start

db = helpers.db


@pytest.mark.parametrize("path", ["refused", "exception", "complete"])
@pytest.mark.parametrize("lease_only", [False, True])
def test_settlement_reuses_agent_without_retiring_or_discarding_successor(db, monkeypatch, path, lease_only):
    _tui_scope(monkeypatch, db)
    monkeypatch.setattr(so, "_gateway_running", lambda home: True)
    helpers._declare_then_reply(db)
    home = db.db_path.parent
    agent = SimpleNamespace(session_id="chat", _last_session_turn_lease_holder="turn-A")
    session = dict(agent=agent, session_key="chat", profile_home=str(home),
                   history_lock=threading.RLock(), running=True)
    assert db.try_acquire_session_turn_lease("chat", "turn-A", ttl_seconds=300)
    successor = []
    retired = []
    refreshed = []
    real_refresh = turn_outcome.refresh_turn_outcome

    def refresh(*a, **kw):
        refreshed.append(kw["concluding_turn"])
        return real_refresh(*a, **kw)

    monkeypatch.setattr(turn_outcome, "refresh_turn_outcome", refresh)
    noop = lambda *a, **kw: None

    def record(*a, **kw):
        assert record_turn_start(home, "chat", "A")
        return "chat"

    def prepare(*a):
        if path == "exception":
            raise ValueError("turn A failed")
        return None if path == "refused" else ("prompt", "message", 80, None)

    real_settle = turn_admission.settle_prompt_state

    def settle(s, st):
        real_settle(s, st)
        assert not s["running"]
        db.release_session_turn_lease("chat", "turn-A")
        _next_turn_starts(db)
        assert db.try_acquire_session_turn_lease("chat", "turn-B", ttl_seconds=300)
        agent._last_session_turn_lease_holder = "turn-B"
        assert record_turn_start(home, "chat", "B")
        successor.append(read_turn_marker(home, "chat"))
        s["running"] = True
        assert so.derive_outcome("chat", db=db)["kind"] == "running"

    def retire(*a, **kw):
        server._retire_turn_marker(*a, **kw)  # real locked marker retirement, not a stub
        retired.append(read_turn_marker(home, "chat"))
        if lease_only:
            # B published its terminal frame, but still owns a lease while unwinding. The marker
            # assertion above and the lease-only outcome assertion below catch each half independently.
            clear_turn_marker(home, "chat")

    class Work:
        def start(self, **kw):
            self.target()
            return True
        def abort(self):
            pass
        def cancel(self):
            pass

    def work(target, **kw):
        result = Work()
        result.target = target
        return result

    monkeypatch.setattr(turn_admission, "settle_prompt_state", settle)
    monkeypatch.setattr(server, "_clear_inflight_turn", noop)
    monkeypatch.setattr(server, "_release_hosted_room_turn_slot", noop)
    namespace = {name: noop for name in (
        "_emit", "bind_transport", "reset_transport", "_release_turn_scopes",
        "_post_turn_housekeeping", "_run_post_turn_followups", "_clear_inflight_turn", "_rearm_carried_pause_watcher", "_emit_settled_session_info",
        "_ensure_session_db_row", "_reopen_routed_session_row", "_recover_turn_exception",
        "_invoke_agent", "_record_stop_notice", "_schedule_refused_turn_retry",
        "_after_complete_turn", "_publish_session_control_snapshot", "_goal_followup_after_turn")}
    namespace.update(threading=threading, time=time, logger=logging.getLogger(__name__),
        _prepare_session_work=work, _sessions_lock=threading.RLock(), _sessions={},
        _admit_prompt_turn=lambda *a, **kw: ([], agent),
        _session_profile_runtime_scope=lambda s: contextlib.nullcontext(),
        _current_runtime_session_record=contextvars.ContextVar("reuse_turn"),
        _TurnRun=prompt_turn._TurnRun, _record_turn_marker=record, _prepare_turn_input=prepare,
        _retire_turn_marker=retire, _routing_provenance_db=lambda s: contextlib.nullcontext(None),
        start_synthesized_turn=server.start_synthesized_turn,
        _absorb_turn_result=lambda *a: None,
        _complete_turn_payload=lambda *a: ({}, "", "complete"))
    submit = rebind(prompt_turn._run_admitted_prompt, namespace)
    assert submit(None, "live", session, "A")
    assert successor
    assert len(refreshed) == 1
    assert "lease:turn-A" in refreshed[0]
    assert "lease:turn-B" not in refreshed[0]
    stored = so.get_outcome("chat", db=db)
    assert (retired, stored["kind"]) == (successor, "running"), (retired, successor, stored)
    assert read_turn_marker(home, "chat") == (None if lease_only else successor[0])


@pytest.mark.parametrize("ownership", ["own", "unknown", "other"])
def test_late_retirement_only_deletes_its_named_marker(db, ownership):
    home = db.db_path.parent
    assert record_turn_start(home, "chat", "own prompt")
    marker = read_turn_marker(home, "chat")
    identity = so.turn_identity(marker=marker) if ownership == "own" else frozenset()
    if ownership == "other":
        identity = so.turn_identity(marker={**marker, "started_at": marker["started_at"] - 1})
    session = dict(session_key="chat", profile_home=str(home))
    server._retire_turn_marker(session, "chat", concluding_turn=identity)
    assert read_turn_marker(home, "chat") == (None if ownership == "own" else marker)
