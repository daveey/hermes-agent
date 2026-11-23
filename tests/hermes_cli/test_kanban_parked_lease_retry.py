"""A primary holder can park before its healthy push loop later stops. [daveey]"""
from __future__ import annotations

from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


@pytest.fixture
def client():
    yield from support.lease_client()


def claimed(board, client):
    sync.set_mode(board, "primary")
    task = kb.create_task(board, title="park on a new helper", assignee="worker")
    assert kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    uid = sync.run_uid(board, run)
    helper = kb.create_task(board, title="new helper", assignee="worker")
    kb.link_tasks(board, helper, task, expected_child_run_id=run)
    return task, run, uid


def released(client):
    return [fields for kind, fields in client.calls if kind == "release"]


def test_dependency_park_retries_release_after_push_stops(board, client, monkeypatch):
    task, run, uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on linked helper", kind="dependency", expected_run_id=run)
    assert kb.get_task(board, task).status == "todo"
    assert not released(client)  # healthy/fresh outbox: no ready window at the park
    # The process exits cleanly now. Its post-commit in-memory queue is gone.
    # No sync push occurs, and the next independent dispatcher sweep is later.
    monkeypatch.setattr(time, "time", lambda: now + 121)
    assert kb.release_stale_claims(board) == 0
    assert [(r["task_uid"], r["run_uid"]) for r in released(client)] == [(task, uid)]
    assert kb.get_task(board, task).consecutive_failures == 0
    assert not [e for e in kb.list_events(board, task) if e.kind in ("failure_finalized", "reclaimed")]
    kb.release_stale_claims(board)
    assert len(released(client)) == 1


def test_obligation_survives_a_new_connection(board, client, monkeypatch):
    from hermes_cli.kanban_db_connect import connect
    task, run, uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(time, "time", lambda: now + 121)
    other = connect(Path(path))
    try:
        kb.release_stale_claims(other)
    finally:
        other.close()
    assert [r["run_uid"] for r in released(client)] == [uid]


def test_failed_retry_is_durable_and_backed_off(board, client, monkeypatch):
    task, run, uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    original = client.release
    attempts = []

    def offline(**fields):
        attempts.append(fields)
        raise sync.LeaseUnavailable("offline fixture")

    monkeypatch.setattr(client, "release", offline)
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    kb.release_stale_claims(board)
    assert len(attempts) == 1
    monkeypatch.setattr(client, "release", original)
    monkeypatch.setattr(time, "time", lambda: now + 242)
    kb.release_stale_claims(board)
    assert [r["run_uid"] for r in released(client)] == [uid]
    assert kb.get_task(board, task).status == "todo"


def test_no_early_retry_and_empty_drain_is_read_only(board, client, monkeypatch):
    task, run, _uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    monkeypatch.setattr(time, "time", lambda: now + 119)
    statements = []
    board.set_trace_callback(statements.append)
    try:
        kb.release_stale_claims(board)
    finally:
        board.set_trace_callback(None)
    assert not released(client)
    assert not [s for s in statements if s.upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE", "CREATE"))]


def test_foreign_principal_cannot_replay_obligation(board, client, monkeypatch):
    task, run, _uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    with kb.write_txn(board):
        board.execute("UPDATE kanban_sync_state SET gateway_id='other-holder'")
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert released(client) == []


def test_savepoint_rollback_keeps_run_and_never_releases(board, client, monkeypatch):
    task, _run, _uid = claimed(board, client)
    now = time.time()
    with kb.write_txn(board):
        board.execute("SAVEPOINT undone")
        kb._end_or_synthesize_run(board, task, outcome="blocked", status="todo", synthesize=False)
        board.execute("ROLLBACK TO undone")
        board.execute("RELEASE undone")
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert released(client) == []


def test_batch_is_bounded_and_foreign_receipts_do_not_starve_it(board, client, monkeypatch):
    from hermes_cli import kanban_run_end_retry as retry
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    expected = []
    # Each receipt corresponds to a distinct committed clean run.
    for _ in range(retry.BATCH_SIZE + 1):
        task, run, uid = claimed(board, client)
        assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
        expected.append(uid)
    with kb.write_txn(board):
        board.execute(f"INSERT INTO {retry.TABLE} SELECT 'foreign-' || run_uid,task_id,'elsewhere',board,0 "
                      f"FROM {retry.TABLE}")
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert len(released(client)) == retry.BATCH_SIZE
    kb.release_stale_claims(board)
    assert sorted(r["run_uid"] for r in released(client)) == sorted(expected)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_retry_cannot_send_outside_primary(board, client, monkeypatch, mode):
    task, run, _uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    sync.set_mode(board, mode)
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert not released(client)


def test_retry_never_sends_inside_a_write_transaction(board, client, monkeypatch):
    task, run, _uid = claimed(board, client)
    now = time.time()
    assert kb.block_task(board, task, reason="wait on helper", kind="dependency", expected_run_id=run)
    monkeypatch.setattr(time, "time", lambda: now + 121)
    with kb.write_txn(board):
        kb.release_stale_claims(board)
        assert not released(client)
    kb.release_stale_claims(board)
    assert len(released(client)) == 1


def test_real_crash_without_park_does_not_get_neutral_release(board, client, monkeypatch):
    task, _run, _uid = claimed(board, client)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert released(client) == []
    assert kb.get_task(board, task).status == "running"


def test_rolled_back_park_has_no_later_release(board, client, monkeypatch):
    task, _run, _uid = claimed(board, client)
    now = time.time()
    with pytest.raises(RuntimeError, match="abort"):
        with kb.write_txn(board):
            kb._end_or_synthesize_run(board, task, outcome="blocked", status="todo", synthesize=False)
            raise RuntimeError("abort")
    monkeypatch.setattr(time, "time", lambda: now + 121)
    kb.release_stale_claims(board)
    assert released(client) == []
