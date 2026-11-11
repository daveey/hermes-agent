"""Neutral owner losses through real Board release/feed and the actual reaper."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync, kanban_sync_client as client
from hermes_cli import kanban_owner_recovery as recovery
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_real_board_failure as bridge_support

board_peer = bridge_support.board_peer


@pytest.mark.parametrize("alarm_first", [False, True])
@pytest.mark.parametrize("reason,event,outcome,flags", [
    ("rate_limited", "rate_limited", "rate_limited", {"rate_limited": True}),
    ("owner_restart", "owner_restart_lost", "reclaimed", {"owner_restart": True}),
    ("owner_unavailable", "owner_unavailable", "owner_unavailable", {"owner_unavailable": True}),
    ("owner_down", "owner_down", "owner_down", {"owner_down": True, "owner_unavailable": True}),
    ("reboot_bystander", "host_reboot", "host_reboot", {"host_reboot": True}),
])
def test_neutral_release_with_split_feed_and_reopened_store(tmp_path, monkeypatch, board_peer,
                                                          alarm_first, reason, event, outcome, flags):
    bridge, hub = board_peer
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    client.ensure_identity(conn, "default", "failure-fixture")
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(client.HubLeaseClient(hub))
    hooks = []
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda *a: True)
    def observe(name, *args, **kwargs):
        if name == "on_kanban_worker_exited":
            hooks.append(kwargs)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", observe)
    try:
        tid = kb.create_task(conn, title="neutral Board fixture", assignee="worker", runs_on=[])
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review', consecutive_failures=1, max_retries=2 WHERE id=?", (tid,))
        client.push(conn, hub, "default", "failure-fixture", primary=True)
        client.pull(conn, hub, "default", observers=True)
        assert kb.claim_review_task(conn, tid, ttl_seconds=900, claimer=kb._host_prefix() + "fixture")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=987654, started_at=1 WHERE id=?", (tid,))
        client.push(conn, hub, "default", "failure-fixture", primary=True)
        client.pull(conn, hub, "default", observers=True)
        rid = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        uid = sync.run_uid(conn, rid)
        monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
        monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
        monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 1)
        monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: None)
        monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
        monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
        monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)
        monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                            dispatch._DeadWorker(reason, 1, "neutral fixture", event, {"fixture": True}, **flags))
        if alarm_first:
            assert bridge.alarm("default", tid, uid)["status"] == 200
        assert dispatch.detect_crashed_workers(conn) == []
        assert dispatch.detect_crashed_workers(conn) == []
        assert not hooks
        client.push(conn, hub, "default", "failure-fixture", primary=True)
        conn.close()
        conn = kbc.connect(path)
        real_changes = hub.changes
        monkeypatch.setattr(hub, "changes", lambda board, since: real_changes(board, since, limit=1))
        real_apply = sync.apply_remote_changes
        def apply_then_reap(*args, **kwargs):
            result = real_apply(*args, **kwargs)
            dispatch.detect_crashed_workers(conn)
            return result
        monkeypatch.setattr(sync, "apply_remote_changes", apply_then_reap)
        client.pull(conn, hub, "default", observers=True)
        dispatch.detect_crashed_workers(conn)
        row = conn.execute("SELECT status,consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()
        assert tuple(row) == (("triage", 2) if alarm_first else ("review", 1))
        assert len(hooks) == (0 if alarm_first else 1)
        if hooks:
            assert hooks[0]["outcome"] == outcome
            assert hooks[0]["retry_status"] == "review"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()[0] == int(alarm_first)
        dispatch.detect_crashed_workers(conn)
        assert len(hooks) == (0 if alarm_first else 1)
    finally:
        sync.set_lease_client(previous)
        conn.close()
