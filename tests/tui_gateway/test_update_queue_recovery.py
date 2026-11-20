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


