"""Recovery of idle update queues is independent of their original retry chain."""
import os
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import admission_retry, server, startup_resume, turn_marker


@pytest.mark.parametrize("refusal", ["retirement", "other"])
def test_retry_survives_one_refused_attempt(tmp_path, monkeypatch, refusal):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    timers = []

    class Timer:
        def __init__(self, delay, target):
            self.target = target
        def start(self):
            timers.append(self)

    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    queued = {"text": "kept", "queued_reason": "core_update"}
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(),
               "running": False, "queued_prompt": queued}
    dispatched = []

    def dispatch(rid, sid, record, envelope, generation):
        dispatched.append(envelope)
        record["running"] = False
        server._clear_inflight_turn(record)
        return True

    monkeypatch.setattr(server, "_dispatch_claimed_queue", dispatch)
    af.set_hold(tmp_path)
    assert server._drain_queued_prompt("r", "s", session)
    if refusal == "retirement":
        with monkeypatch.context() as patch:
            patch.setattr(fence, "acquire", lambda: False)
            timers.pop(0).target()
    else:
        with monkeypatch.context() as patch:
            patch.setattr(server, "_drain_queued_prompt", lambda *args: False)
            timers.pop(0).target()
    af.clear_hold(tmp_path)
    assert len(timers) == 1, "one refusal must not terminate the retry chain"
    timers.pop(0).target()
    assert dispatched == [queued]
    assert not timers
    assert not session.get("queued_prompt")


def test_update_queue_is_not_a_live_turn(tmp_path, monkeypatch):
    import hermes_state_outcome as outcome
    db = SimpleNamespace(db_path=tmp_path / "state.db")
    monkeypatch.setattr(outcome, "_durable_turn_owner", lambda *args: None)
    monkeypatch.setattr(outcome, "_gateway_turn_tokens", lambda *args: iter(()))
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    assert list(outcome._live_turn_tokens(db, "chat")) == []
    assert turn_marker.record_turn_start(tmp_path, "chat", "started")
    assert len(list(outcome._live_turn_tokens(db, "chat"))) == 1


def test_orphan_queue_handoff_with_live_writer(tmp_path, monkeypatch):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    monkeypatch.setattr(server, "_sessions", {})
    admission_retry.redrive_update_queues(tmp_path)
    marker = turn_marker.read_turn_marker(tmp_path, "chat")
    assert marker["writer_pid"] == os.getpid()
    assert turn_marker.marker_writer_state(marker) == "dead"
    pending = startup_resume.pending_resumes(tmp_path, {}, log_skips=False)
    assert [row["session_key"] for row in pending] == ["chat"]


@pytest.mark.parametrize("running", [False, True])
def test_sweep_rearms_only_idle_owned_queue(tmp_path, monkeypatch, running):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = {"session_key": "chat", "profile_home": str(tmp_path),
               "history_lock": threading.RLock(), "running": running,
               "queued_prompt": {"text": "pending", "queued_reason": "core_update"}}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    called = []
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: called.append(a))
    admission_retry.redrive_update_queues(tmp_path)
    assert len(called) == (0 if running else 1)
    assert turn_marker.marker_writer_state(turn_marker.read_turn_marker(tmp_path, "chat")) == "alive"


def test_committed_retirement_hands_queue_to_startup(tmp_path, monkeypatch):
    fence = backend_retirement.RetirementFence()
    from hermes_cli import web_server_idle_proof
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": True})
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = {"session_key": "chat", "profile_home": str(tmp_path),
               "history_lock": threading.RLock(), "queued_prompt": {"text": "pending", "queued_reason": "core_update"}}
    token = fence.prepare()["token"]
    assert not fence.retired()
    assert fence.commit(token)["ok"]
    assert fence.retired()
    assert admission_retry.retry_queued_prompt("r", "s", session) is False
    assert not session.get("_admission_retry")
    assert startup_resume.pending_resumes(tmp_path, {}, log_skips=False)[0]["session_key"] == "chat"


@pytest.mark.parametrize("reason,pid,stale", [(None, None, False), ("update_hold_queue", -1, False),
                                               ("update_hold_queue", None, True)])
def test_release_fences_marker_identity(tmp_path, reason, pid, stale):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason=reason)
    marker = turn_marker.read_turn_marker(tmp_path, "chat")
    if pid is not None:
        turn_marker._update(tmp_path, "chat", lambda entries: {"chat": {**entries["chat"], "writer_pid": pid}}, "fixture")
    if stale:
        marker["started_at"] -= 1
    assert not turn_marker.release_update_queue(tmp_path, "chat", marker)
    assert not turn_marker.read_turn_marker(tmp_path, "chat").get("queue_released")


def test_newer_user_activity_suppresses_replay(tmp_path, monkeypatch):
    from tui_gateway import update_pause
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = {"session_key": "chat", "profile_home": str(tmp_path), "history_lock": threading.RLock()}
    monkeypatch.setattr(update_pause, "later_turns_since", lambda *a: 1)
    assert server._maybe_schedule_auto_continue("s", session, "chat") is None
    assert turn_marker.read_turn_marker(tmp_path, "chat") is None


@pytest.mark.parametrize("state", ["_closing", "_finalized", "missing"])
def test_retry_does_not_revive_discarded_queue(monkeypatch, state):
    session = {"queued_prompt": {"queued_reason": "core_update"}}
    if state == "missing":
        session.pop("queued_prompt")
    else:
        session[state] = True
    monkeypatch.setattr(admission_retry.threading, "Timer", lambda *a: pytest.fail("discarded queue scheduled"))
    assert admission_retry.retry_queued_prompt("r", "s", session) is False


@pytest.mark.parametrize("state", ["_closing", "_finalized", "missing"])
def test_sweep_releases_unusable_owned_queue(tmp_path, monkeypatch, state):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = {"session_key": "chat", "profile_home": str(tmp_path), "history_lock": threading.RLock(),
               "queued_prompt": {"text": "pending", "queued_reason": "core_update"}}
    if state == "missing":
        session.pop("queued_prompt")
    else:
        session[state] = True
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: pytest.fail("unusable queue rearmed"))
    admission_retry.redrive_update_queues(tmp_path)
    marker = turn_marker.read_turn_marker(tmp_path, "chat")
    assert marker is not None and marker.get("queue_released") is True


@pytest.mark.parametrize("change", [{"reason": "core_update_pause"}, {"writer_pid": -1}, {"queue_released": True}])
def test_sweep_preserves_foreign_or_nonqueue_custody(tmp_path, monkeypatch, change):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    turn_marker._update(tmp_path, "chat", lambda entries: {"chat": {**entries["chat"], **change}}, "fixture")
    before = turn_marker.read_turn_marker(tmp_path, "chat")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(turn_marker, "release_update_queue", lambda *a: pytest.fail("foreign/released marker swept"))
    admission_retry.redrive_update_queues(tmp_path)
    assert turn_marker.read_turn_marker(tmp_path, "chat") == before


def test_queue_release_is_idempotent_and_missing_safe(tmp_path):
    assert not turn_marker.release_update_queue(tmp_path, "missing", {})
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    marker = turn_marker.read_turn_marker(tmp_path, "chat")
    assert turn_marker.release_update_queue(tmp_path, "chat", marker)
    assert not turn_marker.release_update_queue(tmp_path, "chat", marker)
    marker["queue_released"] = True
    marker["reason"] = "core_update_pause"
    assert turn_marker.marker_writer_state(marker) == "alive"


def test_newer_user_rows_real_store_suppress_replay(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from hermes_state import SessionDB
    from tui_gateway import update_pause
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("chat", source="desktop")
        assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
        marker = turn_marker.read_turn_marker(tmp_path, "chat")
        session = {"session_key": "chat", "profile_home": str(tmp_path), "history_lock": threading.RLock()}

        @contextmanager
        def session_db(record):
            yield db

        monkeypatch.setattr(server, "_session_db", session_db)
        db.append_message("chat", "user", "pending")
        assert update_pause.later_turns_since(session, "chat", marker["started_at"], "pending") == 0
        db.append_message("chat", "assistant", "old answer")
        assert update_pause.later_turns_since(session, "chat", marker["started_at"], "pending") == 0
        db.append_message("chat", "user", "new request")
        assert update_pause.later_turns_since(session, "chat", marker["started_at"], "pending") == 1
        assert server._maybe_schedule_auto_continue("s", session, "chat") is None
        assert turn_marker.read_turn_marker(tmp_path, "chat") is None
    finally:
        db.close()


def test_stranded_warning_ignores_other_queues_and_resets_for_new_head(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(admission_retry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(backend_retirement, "update_hold_started_at", lambda *a: None)
    session = {"queued_prompt": {"queued_reason": "ordinary"}}
    admission_retry._warn_stranded_queue("s", session)
    assert "_queue_unheld" not in session
    session["queued_prompt"] = {"queued_reason": "core_update"}
    admission_retry._warn_stranded_queue("s", session)
    clock[0] = 60.0
    session["queued_prompt"] = {"queued_reason": "core_update"}
    admission_retry._warn_stranded_queue("s", session)
    assert not caplog.records
    clock[0] = 120.0
    admission_retry._warn_stranded_queue("s", session)
    assert len(caplog.records) == 1


def _owned_queue(tmp_path):
    return {"session_key": "chat", "profile_home": str(tmp_path), "history_lock": threading.RLock(),
            "queued_prompt": {"text": "pending", "queued_reason": "core_update"}}


def test_sweep_inventory_includes_session_created_during_marker_read(tmp_path, monkeypatch):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    sessions = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    load = turn_marker._load

    def load_and_create(path):
        rows = load(path)
        sessions["new"] = _owned_queue(tmp_path)
        return rows

    monkeypatch.setattr(turn_marker, "_load", load_and_create)
    called = []
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: called.append(a))
    admission_retry.redrive_update_queues(tmp_path)
    assert len(called) == 1
    assert not turn_marker.read_turn_marker(tmp_path, "chat").get("queue_released")


@pytest.mark.parametrize("owner_kind", ["queue", "running", "compute", "agent_key", "marker_key"])
def test_stale_alias_cannot_release_live_custody(tmp_path, monkeypatch, owner_kind):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    idle = _owned_queue(tmp_path)
    idle.pop("queued_prompt")
    active = _owned_queue(tmp_path)
    active.update({
        "queue": {},
        "running": {"running": True, "queued_prompt": None},
        "compute": {"_compute_host_turn_id": "turn", "queued_prompt": None},
        "agent_key": {"session_key": "previous", "agent": SimpleNamespace(session_id="chat")},
        "marker_key": {"agent": SimpleNamespace(session_id="new-key")},
    }[owner_kind])
    monkeypatch.setattr(server, "_sessions", {"idle": idle, "active": active})
    called = []
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: called.append(a))
    admission_retry.redrive_update_queues(tmp_path)
    assert len(called) == (1 if owner_kind in {"queue", "agent_key", "marker_key"} else 0)
    assert not turn_marker.read_turn_marker(tmp_path, "chat").get("queue_released")


@pytest.mark.parametrize("closing", [None, "_closing", "_finalized"])
def test_cancelled_live_session_retains_custody_until_teardown(tmp_path, monkeypatch, closing):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = _owned_queue(tmp_path)
    session.pop("queued_prompt")
    session["_turn_cancel_requested"] = True
    if closing:
        session[closing] = True
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: pytest.fail("cancelled queue rearmed"))
    for _ in range(3):
        admission_retry.redrive_update_queues(tmp_path)
        marker = turn_marker.read_turn_marker(tmp_path, "chat")
        assert bool(marker.get("queue_released")) == bool(closing)
        pending = startup_resume.pending_resumes(tmp_path, {}, log_skips=False)
        assert bool(pending) == bool(closing), "a cancelled live runtime cannot execute startup replay"
    # Once the cancelled runtime is gone, the successor may recover teardown custody.
    monkeypatch.setattr(server, "_sessions", {})
    admission_retry.redrive_update_queues(tmp_path)
    assert [r["session_key"] for r in startup_resume.pending_resumes(tmp_path, {}, log_skips=False)] == ["chat"]


@pytest.mark.parametrize("reason", ["update_hold_queue", "core_update_pause", None])
def test_discard_retires_only_queued_marker(tmp_path, reason):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason=reason)
    admission_retry.discard_update_queue_marker(_owned_queue(tmp_path))
    assert (turn_marker.read_turn_marker(tmp_path, "chat") is None) == (reason == "update_hold_queue")


def test_retry_retirement_still_refuses_ordinary_queue(monkeypatch):
    monkeypatch.setattr(backend_retirement.retirement, "acquire", lambda: False)
    assert admission_retry.retry_queued_prompt("r", "s", {"queued_prompt": {"text": "normal"}}) is False


@pytest.mark.parametrize("state", ["running", "_closing", "_finalized", "empty"])
def test_timer_rechecks_state_before_drain(tmp_path, monkeypatch, state):
    session = _owned_queue(tmp_path)
    timers = []

    class Timer:
        def __init__(self, delay, target):
            self.target = target
        def start(self):
            timers.append(self)

    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: pytest.fail("inactive queue drained"))
    assert admission_retry.retry_queued_prompt("r", "s", session)
    assert admission_retry.retry_queued_prompt("r", "s", session)
    assert len(timers) == 1
    if state == "empty":
        session.pop("queued_prompt")
    else:
        session[state] = True
    timers.pop().target()
    assert not session.get("_admission_retry")
    assert not timers


@pytest.mark.parametrize("case", ["missing", "foreign", "no_key"])
def test_discard_does_not_touch_missing_or_foreign_markers(tmp_path, case):
    session = _owned_queue(tmp_path)
    if case != "missing":
        assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    if case == "foreign":
        turn_marker._update(tmp_path, "chat", lambda entries: {"chat": {**entries["chat"], "writer_pid": -1}}, "fixture")
    if case == "no_key":
        session.pop("session_key")
    before = turn_marker.read_turn_marker(tmp_path, "chat")
    admission_retry.discard_update_queue_marker(session)
    assert turn_marker.read_turn_marker(tmp_path, "chat") == before


def test_sweep_does_not_borrow_same_key_from_another_profile(tmp_path, monkeypatch):
    assert turn_marker.record_turn_start(tmp_path, "chat", "pending", reason="update_hold_queue")
    session = _owned_queue(tmp_path / "other")
    monkeypatch.setattr(server, "_sessions", {"other": session})
    monkeypatch.setattr(admission_retry, "retry_queued_prompt", lambda *a: pytest.fail("foreign profile rearmed"))
    admission_retry.redrive_update_queues(tmp_path)
    assert turn_marker.read_turn_marker(tmp_path, "chat")["queue_released"] is True


def test_observer_redrives_all_profiles_before_skew_deferral(tmp_path, monkeypatch):
    from gateway import code_skew
    from hermes_cli import config
    stopped = threading.Event()
    homes = [(None, tmp_path), ("named", tmp_path / "profiles" / "named")]
    monkeypatch.setattr(startup_resume, "_homes", lambda home: homes)
    monkeypatch.setattr(startup_resume, "_paused_markers_remain", lambda *a: True)
    monkeypatch.setattr(startup_resume.time, "sleep", lambda *a: None)
    monkeypatch.setattr(config, "load_config", dict)
    calls = []
    monkeypatch.setattr(admission_retry, "redrive_update_queues", lambda home: calls.append(home))

    def skew():
        stopped.set()
        return {"skew": True}

    monkeypatch.setattr(code_skew, "detect_code_skew", skew)
    monkeypatch.setattr(startup_resume, "resume_interrupted_sessions", lambda *a, **k: pytest.fail("skewed backend replayed"))
    startup_resume._observe_pauses(None, tmp_path, {}, stopped)
    assert calls == [home for _, home in homes]


def test_stranded_warning_is_once_per_hold_lift(monkeypatch, caplog):
    session = {"queued_prompt": {"queued_reason": "core_update"}}
    clock = [10.0]
    held = [None]
    monkeypatch.setattr(admission_retry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(backend_retirement, "update_hold_started_at", lambda *a: held[0])
    admission_retry._warn_stranded_queue("s", session)
    clock[0] += 59
    admission_retry._warn_stranded_queue("s", session)
    assert not caplog.records
    clock[0] += 1
    admission_retry._warn_stranded_queue("s", session)
    admission_retry._warn_stranded_queue("s", session)
    assert len(caplog.records) == 1
    assert "60s after hold lift" in caplog.text
    held[0] = 100.0
    admission_retry._warn_stranded_queue("s", session)
    assert "_queue_unheld" not in session


