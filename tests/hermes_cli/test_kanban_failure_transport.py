"""Failure commands must cross the sync boundary as operations, not counter rows."""
import json

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, "default", "fixture")
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


def _running(conn):
    tid = kb.create_task(conn, title="failure transport fixture", assignee="worker")
    with kbc.write_txn(conn):
        rid = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                           "VALUES (?, 'worker', 'running', 1)", (tid,)).lastrowid
        conn.execute("UPDATE tasks SET status='running', current_run_id=?, sync_version=5 WHERE id=?", (rid, tid))
        conn.execute("DELETE FROM kanban_sync_outbox")
    return tid, rid


def test_queue_release_is_durable_run_scoped_and_does_not_write_mirrors(live):
    from hermes_cli import kanban_failure_release as release
    tid, rid = _running(live)
    before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    with kbc.write_txn(live):
        assert release.queue(live, tid, rid, outcome="crashed", error="exit 1")
        assert release.queue(live, tid, rid, outcome="crashed", error="late exit")
    rows = list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert len(rows) == 1
    assert rows[0]["op"] == "release"
    assert json.loads(rows[0]["fields"])["run_uid"] == sync.run_uid(live, rid)
    assert dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert live.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()[0] == "running"
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")  # acknowledged: receipt still prevents a second command
        assert release.queue(live, tid, rid, outcome="crashed", error="again")
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_queue_release_only_primary(live, mode):
    from hermes_cli import kanban_failure_release as release
    tid, rid = _running(live)
    sync.set_mode(live, mode)
    with kbc.write_txn(live):
        assert not release.queue(live, tid, rid, outcome="crashed", error="exit 1")
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))


@pytest.mark.parametrize("bad_run", [None, -1])
def test_primary_missing_identity_never_falls_back_to_local_accounting(live, bad_run):
    from hermes_cli import kanban_failure_release as release
    tid, _ = _running(live)
    with kbc.write_txn(live), pytest.raises(ValueError, match="portable run identity"):
        release.queue(live, tid, bad_run, outcome="crashed", error="exit")
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))


def test_primary_rejects_foreign_run_and_nontransactional_queue(live):
    from hermes_cli import kanban_failure_release as release
    tid, rid = _running(live)
    with kbc.write_txn(live), pytest.raises(ValueError, match="another task"):
        release.queue(live, "t_other", rid, outcome="crashed", error="exit")
    with pytest.raises(ValueError, match="write transaction"):
        release.queue(live, tid, rid, outcome="crashed", error="exit")
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))


def test_receipt_and_command_roll_back_together_and_new_run_can_queue(live):
    from hermes_cli import kanban_failure_release as release
    tid, rid = _running(live)
    with pytest.raises(RuntimeError), kbc.write_txn(live):
        release.queue(live, tid, rid, outcome="crashed", error="exit")
        raise RuntimeError("abort")
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))
    with kbc.write_txn(live):
        assert release.queue(live, tid, rid, outcome="crashed", error="exit")
        rid2 = live.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                            "VALUES (?, 'worker', 'running', 2)", (tid,)).lastrowid
        live.execute("DELETE FROM kanban_sync_outbox WHERE op != 'release'")
        assert release.queue(live, tid, rid2, outcome="crashed", error="new exit")
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2
    assert live.execute("SELECT COUNT(*) FROM kanban_failure_commands").fetchone()[0] == 2


def test_release_ack_does_not_hide_authoritative_task_feed(live):
    tid, rid = _running(live)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                     "VALUES ('release-fixture','tasks',?,'release',?,'',1)",
                     (tid, json.dumps({"run_uid": sync.run_uid(live, rid), "outcome": "crashed"})))
    class Hub:
        def ops(self, board, wire):
            assert len(wire) == 1 and wire[0]["op"] == "release"
            return {"results": [{"client_op_id": "release-fixture", "status": "applied", "version": 9}]}
    assert client.push(live, Hub(), "default", "fixture", primary=True).applied == 1
    assert live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 5


def test_pending_release_is_not_a_local_task_edit(live):
    tid, rid = _running(live)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                     "VALUES ('release-fixture','tasks',?,'release',?,'',1)",
                     (tid, json.dumps({"run_uid": sync.run_uid(live, rid), "outcome": "crashed", "error": "exit"})))
    assert client.pending_task_fields(live) == {}
    change = sync.RemoteChange(seq=1, table="tasks", uid=tid, op="upsert", version=6,
                               hlc="", origin_gateway="board", fields={"error": "authoritative"})
    assert sync._without_unacked_fields(live, change) == change.fields


@pytest.mark.parametrize("force_trip,infrastructure", [(False, False), (True, False), (False, True)])
def test_dispatch_failure_queues_before_mutating_primary_mirror(live, force_trip, infrastructure):
    from hermes_cli import kanban_db_dispatch as dispatch
    tid, rid = _running(live)
    before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    for _ in range(2):
        assert not dispatch._record_task_failure(
            live, tid, error="spawn refused", outcome="spawn_failed", release_claim=True,
            end_run=True, force_trip=force_trip, infrastructure=infrastructure,
            failure_limit=2, event_payload_extra={"fixture": True}, block_kind="assignment",
        )
    assert dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert live.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()[0] == "running"
    rows = list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert len(rows) == 1 and rows[0]["op"] == "release"
    fields = json.loads(rows[0]["fields"])
    assert fields == {"run_uid": sync.run_uid(live, rid), "outcome": "spawn_failed",
                      "error": "spawn refused", "force_trip": force_trip,
                      "infrastructure": infrastructure, "failure_limit": 2,
                      "extra": {"fixture": True}, "block_kind": "assignment"}


@pytest.mark.parametrize("flag", [None, "terminal_provider", "unknown_skill", "stdlib_shadow", "reboot_culprit", "protocol_violation"])
def test_primary_real_crash_sweep_queues_once_without_provisional_state(live, monkeypatch, flag):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = _running(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=987654, claim_lock=?, started_at=1 WHERE id=?",
                     (kb._host_prefix() + "fixture", tid))
        live.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 1)
    observed = []
    monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: observed.append(a[1]))
    monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
    monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, "comment_culprit", lambda *a: None)
    monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)
    flags = {flag: ({"fixture": True} if flag == "reboot_culprit" else True)} if flag else {}
    def dead_worker(*args, **kwargs):
        dead = dispatch._DeadWorker("crashed", 1, "fixture exit 1", "crashed", {"exit_code": 1})
        for key, value in flags.items():
            setattr(dead, key, value)
        return dead
    monkeypatch.setattr(dispatch, "_classify_dead_worker", dead_worker)
    before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert dispatch.detect_crashed_workers(live) == [tid]
    assert dispatch.detect_crashed_workers(live) == []
    assert observed == [tid]
    assert dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    rows = list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert len(rows) == 1 and rows[0]["op"] == "release"
    fields = json.loads(rows[0]["fields"])
    assert fields["run_uid"] == sync.run_uid(live, rid)
    if flag == "protocol_violation":
        assert fields["protocol_violation"] is True
        assert fields["failure_limit"] == 3
        assert "force_trip" not in fields
    else:
        assert fields["force_trip"] is bool(flag)
    if flag and flag != "protocol_violation":
        assert fields["extra"][flag] == flags[flag]
    assert not list(live.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN ('crashed','gave_up')", (tid,)))


@pytest.mark.parametrize("mode,flags,expected", [
    ("off", {}, False), ("shadow", {}, False), ("primary", {}, True),
    ("primary", {"protocol_violation": True}, True), ("primary", {"rate_limited": True}, False),
    ("primary", {"owner_restart": True}, False), ("primary", {"owner_unavailable": True}, False),
    ("primary", {"host_reboot": True}, False),
    ("primary", {"host_reboot": True, "reboot_culprit": {"fixture": True}}, True),
    ("primary", {"host_reboot": True, "reboot_culprit": {"fixture": True}, "owner_unavailable": True}, True),
    ("primary", {"host_reboot": True, "reboot_culprit": {"fixture": True}, "owner_restart": True}, True),
])
def test_deferred_crash_retains_exemptions(live, mode, flags, expected):
    from hermes_cli.kanban_db_dispatch_dead_worker import _DeadWorker
    from hermes_cli.kanban_failure_release import deferred_crash
    sync.set_mode(live, mode)
    dead = _DeadWorker("crashed", 1, "fixture", "crashed", {})
    for key, value in flags.items():
        setattr(dead, key, value)
    assert deferred_crash(live, dead) is expected


@pytest.mark.parametrize("path", ["timeout", "stale"])
def test_primary_timeout_and_stale_do_not_publish_competing_state(live, monkeypatch, path):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = _running(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=987654, claim_lock=?, claim_expires=1, "
                     "max_runtime_seconds=1, started_at=1 WHERE id=?", (kb._host_prefix() + "fixture", tid))
        live.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(kb, "_pid_alive", lambda *a: False)
    monkeypatch.setattr(kb, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(dispatch, "_poll_worker_exit", lambda *a: None)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked_after_kill", lambda *a, **kw: False)
    monkeypatch.setattr(sync, "lease_lost", lambda *a: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **kw: {})
    before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    signals = []
    for attempt in range(2):
        if path == "timeout":
            assert dispatch.enforce_max_runtime(live, signal_fn=lambda *a: signals.append(a)) == ([tid] if attempt == 0 else [])
        else:
            assert kb.release_stale_claims(live) == 0  # Board alarm owns expired leases.
    assert len(signals) == (1 if path == "timeout" else 0)
    assert dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert live.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()[0] == "running"
    commands = list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert len(commands) == (1 if path == "timeout" else 0)
    if commands:
        fields = json.loads(commands[0]["fields"])
        assert fields["outcome"] == "timed_out" and fields["run_uid"] == sync.run_uid(live, rid)


@pytest.mark.parametrize("changed", ["status", "worker_pid", "claim_lock", "current_run_id"])
def test_timeout_revalidates_attempt_after_kill_wait(live, monkeypatch, changed):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = _running(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=987654, claim_lock=?, max_runtime_seconds=1 WHERE id=?",
                     (kb._host_prefix() + "fixture", tid))
        live.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(kb, "_pid_alive", lambda *a: False)
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked_after_kill", lambda *a, **kw: False)
    def advance(*args):
        value = {"status": "ready", "worker_pid": 987655, "claim_lock": "new:claim", "current_run_id": None}[changed]
        with kbc.write_txn(live):
            live.execute(f"UPDATE tasks SET {changed}=? WHERE id=?", (value, tid))
            live.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(dispatch, "_poll_worker_exit", advance)
    assert dispatch.enforce_max_runtime(live, signal_fn=lambda *a: None) == []
    assert not list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert live.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()[0] == "running"
    assert live.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 0


def test_queued_receipt_is_run_scoped_primary_only_and_cleans_up(live):
    from hermes_cli import kanban_failure_release as release
    tid, rid = _running(live)
    assert not release.queued(live, rid)
    with kbc.write_txn(live):
        release.queue(live, tid, rid, outcome="crashed", error="exit")
        live.execute("DELETE FROM kanban_sync_outbox")
    assert release.queued(live, rid)
    assert not release.queued(live, None)
    other_tid, other_rid = _running(live)
    assert other_tid != tid and not release.queued(live, other_rid)
    for mode in ("off", "shadow"):
        sync.set_mode(live, mode)
        assert not release.queued(live, rid)
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("DELETE FROM tasks WHERE id=?", (tid,))
    assert not list(live.execute("SELECT * FROM kanban_failure_commands"))


def test_release_outbox_keeps_run_identity_and_policy():
    from hermes_cli import kanban_sync_wire as wire_ops
    fields = {"run_uid": "run-fixture", "outcome": "crashed", "error": "exited 1",
              "force_trip": True, "failure_limit": 2}
    wire = wire_ops.translate({"op_id": "failure-fixture", "table_name": "tasks",
                             "row_uid": "t_fixture", "op": "release", "fields": json.dumps(fields)}, "unused")
    assert wire == {"client_op_id": "failure-fixture", "op": "release", "task": "t_fixture", **fields}
