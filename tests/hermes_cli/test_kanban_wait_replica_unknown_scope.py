"""An owner that publishes its board scope as ``unknown`` is "unknown, not yet", never met and never a broken probe.

The hermes-hub collector (``client/hermes_hub.py`` ``collect_all`` + ``scope_heartbeats``) publishes
``{"profile", "board", "state": "unknown"}`` with no ``observed_at`` when it could not read that board for longer
than its 450 s carry window. The task records it published before stay in the replica, so the waiter knows the
last-seen status but nothing vouches for it now: the same "cannot tell" as a stale (offline) replica. [daveey]
"""
import json
import time

import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_wake_check as wake
from tests.hermes_cli import test_kanban_wait_replica as base

TID = base.TID
replica = base.replica
board = base.board
put = base.put
make_stale = base.make_stale
probe_results = base._probe_results
DAY = wake.STALE_WAKE_AFTER


def set_scopes(db, *scopes):
    db.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps({"task_scopes": list(scopes)}),))
    db.commit()


def producer_unknown(profile="default", board_name="default"):
    """Exactly the record the hermes-hub collector writes for a scope whose read failed past its carry window."""
    records = None  # collect_tasks failed: the source is not tombstoned
    scope = {"profile": profile, "board": board_name, "state": "unknown" if records is None else "ok"}
    if records is not None:
        scope["observed_at"] = time.time()
    return scope


def make_unknown(db):
    set_scopes(db, producer_unknown())


@pytest.mark.parametrize("status", ["done", "running", "blocked", "archived"])
def test_unknown_scope_is_unknown_not_yet_never_met(replica, status):
    put(replica, status)
    make_unknown(replica)
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    assert rc == 3, out
    assert out.split()[:2] == [TID, status]
    assert "source_state=unknown" in out and "gateway=other" in out
    assert "stale_age=" not in out  # no observation time exists: never invent one
    assert not wake.output_error(out)
    assert wake.source_unknown(rc, out) == "other"
    assert wake.stale_age(rc, out) is None


@pytest.mark.parametrize("scopes", [
    [],  # nothing published for this board
    [{"profile": "default", "board": "default", "state": "unknown"}] * 2,  # ambiguous
    [{"profile": "default", "board": "default", "state": "error"}],  # unrecognised state
    [{"profile": "default", "board": "default", "state": None}],
    [{"profile": "default", "board": "default"}],
    [{"profile": "default", "board": "default", "state": "unknown", "observed_at": 1}],  # contradictory
    [{"profile": "default", "board": "default", "state": "ok", "observed_at": time.time()},
     {"profile": "default", "board": "default", "state": "unknown"}],
])
def test_malformed_or_ambiguous_scopes_stay_errors(replica, scopes):
    put(replica, "done")
    set_scopes(replica, *scopes)
    rc, out = wait.check_remote("other", "task", TID)
    assert rc == 2 and out.startswith("error: replica other:"), out
    assert wake.source_unknown(rc, out) is None


def test_unknown_scope_on_missing_or_invalid_task_stays_error(replica):
    make_unknown(replica)
    assert wait.check_remote("other", "task", TID)[0] == 2  # nothing to have seen
    put(replica, "done")
    assert wait.check_remote("other", "task", TID, board="elsewhere")[0] == 2


def test_other_profile_unknown_does_not_shadow_fresh_scope(replica):
    put(replica, "done")
    set_scopes(replica, {"profile": "default", "board": "default", "state": "ok", "observed_at": time.time()},
               producer_unknown(profile="household"))
    assert wait.check_remote("other", "task", TID, until=("done",))[0] == 0


def test_lookalike_output_is_not_source_unknown():
    assert wake.source_unknown(2, "x running (replica scope unknown: gateway=other source_state=unknown)") is None
    assert wake.source_unknown(3, "error: replica other: source_state=unknown gateway=other") is None
    assert wake.source_unknown(3, "x running (source_state=unknown)") is None  # no gateway named
    assert wake.source_unknown(3, "x running (gateway=other)") is None
    # A stale-age result is the offline path, never the scope-unknown one.
    assert wake.source_unknown(3, "x running (gateway=other stale_age=9s source_state=unknown)") is None


def test_offline_after_acknowledged_unknown_is_the_same_source_state(replica, board, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    now = int(base.pin_replica_clock(monkeypatch))
    put(replica, "running")
    make_unknown(replica)
    cmd = f"hermes kanban wait-check task {TID} --gateway other"
    rc, out = wait.check_remote("other", "task", TID)
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now)
    assert _broken(conn, tid)[-1]["reason"] == "gateway_scope_unknown"
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    _park(conn, tid, cmd)
    make_stale(replica, DAY + 3600)  # then the owner stops syncing entirely
    rc, out = wait.check_remote("other", "task", TID)
    assert wake.stale_age(rc, out) is not None
    probe_results(conn, tid, cmd, rc, out, 1, start=now + 900)
    assert kb.get_task(conn, tid).status == "scheduled" and len(_broken(conn, tid)) == 1


def test_dry_run_accepts_unknown_scope_park(replica, monkeypatch):
    put(replica, "running")
    make_unknown(replica)
    monkeypatch.delenv("HERMES_KANBAN_WAKE_DRYRUN", raising=False)
    rc, out = wait.check_remote("other", "task", TID)
    monkeypatch.setattr(wake, "_run_probe", lambda *a, **kw: (rc, out))
    assert wake.dry_run_error(f"hermes kanban wait-check task {TID} --gateway other") is None
    monkeypatch.setattr(wake, "_run_probe", lambda *a, **kw: (2, "error: replica other: missing scope"))
    assert "exited 2" in wake.dry_run_error(f"hermes kanban wait-check task {TID} --gateway other")


def _park(conn, tid, cmd, every=900):
    from hermes_cli import kanban_db as kb
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", wake_check=cmd,
                            check_every=every)


def _broken(conn, tid):
    from hermes_cli import kanban_db as kb
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == wake.BROKEN_EVENT]


def test_unknown_scope_escalates_once_then_backs_off_across_repark(replica, board, monkeypatch):
    """The observed incident: wake -> claim -> repark on the same probe must not re-wake every 3 probes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn, tid = board
    now = int(base.pin_replica_clock(monkeypatch))
    put(replica, "running")
    make_unknown(replica)
    cmd = f"hermes kanban wait-check task {TID} --gateway other --until done,blocked"
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now)
    # Initial escalation: one typed wake, not a "broken probe" after BROKEN_AFTER faults.
    assert kb.get_task(conn, tid).status == "ready"
    first = _broken(conn, tid)
    assert len(first) == 1 and first[0]["reason"] == "gateway_scope_unknown" and first[0]["gateway"] == "other"
    assert first[0]["fails"] == 0
    context = wake.render_wake_context(conn, tid)
    assert "could not read its board" in context and "Fix the probe" not in context
    # Acknowledged: the worker re-parks on the same probe. Unchanged unknown results never wake within 24 h.
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, wake.BROKEN_AFTER + 3, start=now + 60)
    assert kb.get_task(conn, tid).status == "scheduled"
    state = wake.get_state(conn, tid)
    assert state["last_rc"] == 3 and state["fails"] == 0 and state["next_at"] > now
    # Durable receipt: a fresh connection (another dispatcher process) sees the acknowledgement.
    with kbc.connect_closing() as reopened:
        probe_results(reopened, tid, cmd, rc, out, 1, start=now + 7200)
        assert kb.get_task(reopened, tid).status == "scheduled"
    assert len(_broken(conn, tid)) == 1
    # Bounded: still unknown a day later -> one more escalation, never a silent forever.
    probe_results(conn, tid, cmd, rc, out, 1, start=now + DAY + 1)
    assert kb.get_task(conn, tid).status == "ready" and len(_broken(conn, tid)) == 2


def test_unknown_after_acknowledged_offline_is_the_same_source_state(replica, board, monkeypatch):
    """The detector was retained on rc 3 after a gateway_offline wake; the owner's scope then turned unknown."""
    from hermes_cli import kanban_db as kb
    conn, tid = board
    now = int(base.pin_replica_clock(monkeypatch))
    put(replica, "running")
    make_stale(replica, DAY + 3600)
    cmd = f"hermes kanban wait-check task {TID} --gateway other --until done,blocked"
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now)
    assert _broken(conn, tid)[-1]["reason"] == "gateway_offline"
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    _park(conn, tid, cmd)
    make_unknown(replica)
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    probe_results(conn, tid, cmd, rc, out, wake.BROKEN_AFTER + 1, start=now + 900)
    assert kb.get_task(conn, tid).status == "scheduled" and len(_broken(conn, tid)) == 1


@pytest.mark.parametrize("change", ["probe", "gateway"])
def test_backoff_is_scoped_to_probe_and_gateway(replica, board, monkeypatch, change):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    now = int(base.pin_replica_clock(monkeypatch))
    put(replica, "running")
    make_unknown(replica)
    cmd = f"hermes kanban wait-check task {TID} --gateway other"
    rc, out = wait.check_remote("other", "task", TID)
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now)
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    if change == "probe":
        cmd += " --until done"
    else:
        out = out.replace("gateway=other", "gateway=third")
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now + 60)
    assert kb.get_task(conn, tid).status == "ready" and len(_broken(conn, tid)) == 2


@pytest.mark.parametrize("next_result", ["done", "fault", "fresh_running"])
def test_recovery_and_fault_controls_after_backoff(replica, board, monkeypatch, next_result):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    now = int(base.pin_replica_clock(monkeypatch))
    put(replica, "running")
    make_unknown(replica)
    cmd = f"hermes kanban wait-check task {TID} --gateway other --until done,blocked"
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 1, start=now)
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    _park(conn, tid, cmd)
    probe_results(conn, tid, cmd, rc, out, 2, start=now + 60)
    assert kb.get_task(conn, tid).status == "scheduled"
    if next_result == "fault":
        # A genuine probe fault is never swallowed by the unknown-scope backoff.
        probe_results(conn, tid, cmd, 2, "error: replica other: ambiguous replica records", wake.BROKEN_AFTER,
                      start=now + 3600)
        assert kb.get_task(conn, tid).status == "ready"
        last = _broken(conn, tid)[-1]
        assert last.get("reason") is None and last["fails"] == wake.BROKEN_AFTER
        return
    # The owner reads its board again: a fresh successful scope.
    set_scopes(replica, {"profile": "default", "board": "default", "state": "ok", "observed_at": time.time()})
    if next_result == "done":
        replica.execute("DELETE FROM replica WHERE coll='tasks'")
        put(replica, "done")
    rc, out = wait.check_remote("other", "task", TID, until=("done", "blocked"))
    probe_results(conn, tid, cmd, rc, out, 1, start=now + 3600)
    if next_result == "done":
        assert rc == 0 and kb.get_task(conn, tid).status == "ready"
        assert any(e.kind == wake.CONDITION_MET_EVENT for e in kb.list_events(conn, tid))
    else:
        assert rc == 1 and kb.get_task(conn, tid).status == "scheduled"
        assert wake.get_state(conn, tid)["last_rc"] == 1


def test_confirm_remote_accepts_unknown_scope_without_claiming_completion(replica, board, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    monkeypatch.setattr(awaiting, "_REMOTE_CONFIRMED", {})
    put(replica, "running")
    make_unknown(replica)

    # Real confirm_remote -> check_remote -> replica reader, not a canned rc=3.
    awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other", board="default"))
    _, observed = awaiting._REMOTE_CONFIRMED[("other", TID)]
    assert f"{TID} running" in observed and "source_state=unknown" in observed
    assert "stale_age=" not in observed

    res = base._block_on_remote_card(conn, tid, monkeypatch)
    assert res.get("status") == "scheduled", res
    assert kb.get_task(conn, tid).status == "scheduled"
    events = kb.list_events(conn, tid)
    assert "source_state=unknown" in base.parked_observation(events)
    assert not any(e.kind == wake.CONDITION_MET_EVENT for e in events)
    assert wait.check_remote("other", "task", TID)[0] == 3


@pytest.mark.parametrize("scope_state", ["unknown", "ok"])
def test_confirm_remote_rejects_last_seen_done_producer(replica, board, monkeypatch, scope_state):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    monkeypatch.setattr(awaiting, "_REMOTE_CONFIRMED", {})
    put(replica, "done")
    if scope_state == "unknown":
        make_unknown(replica)
    assert wait.check_remote("other", "task", TID)[0] == (3 if scope_state == "unknown" else 0)

    with pytest.raises(awaiting.AwaitingError) as exc:
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other", board="default"))
    expected = awaiting.producer_done_error(TID, "per other's board")
    assert exc.value.code == expected.code == awaiting.PRODUCER_DONE
    assert str(exc.value) == str(expected)
    assert ("other", TID) not in awaiting._REMOTE_CONFIRMED

    res = base._block_on_remote_card(conn, tid, monkeypatch)
    assert awaiting.PRODUCER_DONE in json.dumps(res), res
    assert kb.get_task(conn, tid).status == "running"
    assert not any(e.kind == "scheduled" for e in kb.list_events(conn, tid))


def test_unknown_scope_keeps_last_seen_completed_at(replica):
    put(replica, "done", completed_at=1234)
    make_unknown(replica)
    assert wake._remote_completed_at("other", TID, None) == 1234
