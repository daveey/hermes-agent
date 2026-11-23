"""A paged replica is not a legacy executor missing its run ledger."""

from hermes_cli import kanban_db_connect as connect
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_cross_gateway_parents as fixtures

kanban_home = fixtures.kanban_home
live = fixtures.live
svc = fixtures.svc


def test_reopening_replica_between_task_and_run_pages_does_not_invent_run(live, kanban_home):
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    path = client.shadow_path(fixtures.BOARD)
    try:
        sync.apply_remote_changes(replica, [sync.RemoteChange(
            1, "tasks", "t_paged", "upsert",
            {"title": "paged", "status": "running", "created_at": 1,
             "started_at": 2, "assignee": "default", "created_on": fixtures.OTHER},
            59, "", fixtures.OTHER,
        )], new_cursor=1, observers=False)
        before = [tuple(row) for row in replica.execute("SELECT * FROM kanban_sync_outbox")]
    finally:
        replica.close()
    connect._INITIALIZED_PATHS.discard(str(path.resolve()))
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        assert replica.execute("SELECT current_run_id FROM tasks WHERE id='t_paged'").fetchone()[0] is None
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE task_id='t_paged'").fetchone()[0] == 0
        assert [tuple(row) for row in replica.execute("SELECT * FROM kanban_sync_outbox")] == before
        assert sync.sync_state(replica)["cursor"] == 1
        sync.apply_remote_changes(replica, [sync.RemoteChange(
            2, "task_runs", "canonical-run", "upsert",
            {"task_id": "t_paged", "profile": "default", "status": "running", "started_at": 2},
            1, "", fixtures.OTHER,
        ), sync.RemoteChange(
            3, "tasks", "t_paged", "upsert", {"current_run_uid": "canonical-run"},
            60, "", fixtures.OTHER,
        )], new_cursor=3, observers=False, local_pending_wins=True)
        row = replica.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
                              "WHERE t.id='t_paged'").fetchone()
        assert row[0] == "canonical-run"
        assert [tuple(row) for row in replica.execute("SELECT * FROM kanban_sync_outbox")] == before
    finally:
        replica.close()


def test_primary_legacy_backfill_still_preserves_local_executor(live):
    with connect.write_txn(live):
        live.execute("INSERT INTO tasks (id,title,status,created_at,started_at,assignee,claim_lock,claim_expires) "
                     "VALUES ('t_legacy','legacy','running',1,2,'default','executor',9000000000)")
    connect._backfill_legacy_inflight_runs(live)
    row = live.execute("SELECT r.status,r.claim_lock,r.claim_expires,r.started_at FROM tasks t "
                       "JOIN task_runs r ON r.id=t.current_run_id WHERE t.id='t_legacy'").fetchone()
    assert tuple(row) == ("running", "executor", 9000000000, 2)
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid='t_legacy'").fetchone()
    connect._backfill_legacy_inflight_runs(live)
    assert live.execute("SELECT COUNT(*) FROM task_runs WHERE task_id='t_legacy'").fetchone()[0] == 1
