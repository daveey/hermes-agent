"""Primary owner/reboot losses wait for Board evidence before exit observers."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync, kanban_owner_recovery as recovery
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


@pytest.mark.parametrize("reason,event,outcome,flags", [
    ("rate_limited", "rate_limited", "rate_limited", {"rate_limited": True}),
    ("owner_restart", "owner_restart_lost", "reclaimed", {"owner_restart": True}),
    ("owner_unavailable", "owner_unavailable", "owner_unavailable", {"owner_unavailable": True}),
    ("owner_down", "owner_down", "owner_down", {"owner_down": True, "owner_unavailable": True}),
    ("reboot_bystander", "host_reboot", "host_reboot", {"host_reboot": True}),
])
def test_neutral_sweep_queues_without_mirror_edits_and_drains_after_feed(live, monkeypatch, reason, event, outcome, flags):
    tid, rid = support._running(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=987654, claim_lock=?, started_at=1 WHERE id=?",
                     (kb._host_prefix() + "fixture", tid))
        live.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 1)
    monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
    monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
    monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)
    dead = dispatch._DeadWorker(reason, 1, "fixture neutral exit", event, {"fixture": True}, **flags)
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw: dead)
    hooks = []
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda *a: True)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **kw: hooks.append(kw))
    before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert dispatch.detect_crashed_workers(live) == []
    assert dispatch.detect_crashed_workers(live) == []
    assert dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    commands = list(live.execute("SELECT fields FROM kanban_sync_outbox WHERE op='release'"))
    assert len(commands) == 1
    fields = json.loads(commands[0][0])
    assert fields["outcome"] == reason
    assert fields["extra"]["fixture"] is True
    assert not hooks
    # Command ACK / task row alone are insufficient; the end and event can arrive separately.
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
        live.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (tid,))
    dispatch.detect_crashed_workers(live)
    assert not hooks
    with kbc.write_txn(live):
        live.execute("UPDATE task_runs SET ended_at=2, outcome=?, status=? WHERE id=?", (outcome, outcome, rid))
    dispatch.detect_crashed_workers(live)
    assert not hooks
    with kbc.write_txn(live):
        kb._append_event(live, tid, event, {"retry_status": "ready"}, run_id=rid)
    dispatch.detect_crashed_workers(live)
    assert len(hooks) == 1
    assert hooks[0]["run_id"] == rid and hooks[0]["outcome"] == outcome
    dispatch.detect_crashed_workers(live)
    assert len(hooks) == 1
    from hermes_cli.kanban_neutral_release import drain, pending
    assert not pending(live)
    statements = []
    live.set_trace_callback(statements.append)
    try:
        assert drain(live) == []
    finally:
        live.set_trace_callback(None)
    assert not any(s.upper().startswith("BEGIN") for s in statements)
    assert live.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 0
    assert not list(live.execute("SELECT 1 FROM task_events WHERE kind IN ('gave_up','failure_finalized')"))


def test_neutral_reason_precedence_and_local_controls(live):
    from hermes_cli.kanban_neutral_release import reason_for
    dead = dispatch._DeadWorker("fixture", 1, "fixture", "crashed", {}, rate_limited=True, owner_restart=True)
    assert reason_for(dead) == "owner_restart"
    dead.owner_unavailable = True
    assert reason_for(dead) == "owner_unavailable"
    dead.owner_down = True
    assert reason_for(dead) == "owner_down"
    dead.host_reboot = True
    assert reason_for(dead) == "reboot_bystander"
    dead.reboot_culprit = {"fixture": True}
    assert reason_for(dead) is None
    assert reason_for(dispatch._DeadWorker("rate_limited", 1, "quota", "rate_limited", {}, rate_limited=True)) == "rate_limited"
    from hermes_cli.kanban_neutral_release import queue
    tid, rid = support._running(live)
    dead.run_id = rid
    dead.reboot_culprit = None
    row = live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    for mode in ("off", "shadow"):
        sync.set_mode(live, mode)
        with kbc.write_txn(live):
            assert not queue(live, row, dead)
