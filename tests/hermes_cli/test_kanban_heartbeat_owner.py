"""heartbeat_claim must not touch the Board lease for a claim the caller no longer owns."""
from __future__ import annotations

import logging
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def lease_client():
    yield from support.lease_client()


@pytest.fixture
def primary(kanban_home):
    for conn in support.synced_conn(kanban_home):
        sync.set_mode(conn, "primary")
        yield conn


OLD, NEW = "host:old", "host:new"


def _claimed_again(conn, lease_client):
    """Claim with OLD, end that run (continued), claim again with NEW; leave the lease due for renewal."""
    tid = kb.create_task(conn, title="moved on", assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=OLD) is not None
    now = int(time.time())
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_runs SET status = 'continued', outcome = 'continued', ended_at = ? "
                     "WHERE task_id = ? AND ended_at IS NULL", (now, tid))
        conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                     "current_run_id = NULL, worker_pid = NULL WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=NEW) is not None
    new_uid = conn.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                           (tid,)).fetchone()[0]
    with kbc.write_txn(conn):  # a third of the lease used up: a renewal is due
        conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int((time.time() + 200) * 1000), tid))
    sync._RENEW_ATTEMPTS.pop(tid, None)
    lease_client.calls.clear()
    return tid, new_uid


def test_ended_worker_heartbeat_makes_no_renewal_for_the_next_run(primary, lease_client):
    tid, _new_uid = _claimed_again(primary, lease_client)
    assert kb.heartbeat_claim(primary, tid, ttl_seconds=900, claimer=OLD) is False
    assert lease_client.count("renew") == 0, lease_client.calls


def test_ended_worker_heartbeat_logs_no_false_lease_lost(primary, lease_client, caplog):
    tid, _new_uid = _claimed_again(primary, lease_client)
    holder = primary.execute("SELECT lease_holder FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    lease_client.renew_mode = "deny"  # the Board refuses a renew from a process that is not the holder's run
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        kb.heartbeat_claim(primary, tid, ttl_seconds=900, claimer=OLD)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert primary.execute("SELECT lease_holder FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == holder


def test_owner_heartbeat_still_renews_when_due(primary, lease_client):
    tid, new_uid = _claimed_again(primary, lease_client)
    assert kb.heartbeat_claim(primary, tid, ttl_seconds=900, claimer=NEW) is True
    assert [(k, kw["run_uid"]) for k, kw in lease_client.calls] == [("renew", new_uid)]
