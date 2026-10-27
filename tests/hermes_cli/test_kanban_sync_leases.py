"""Lease seams (core patch 0193): a ``primary`` synced board claims only with a
server-granted lease, renews it at most once per ``ttl/3``, and a worker that
cannot renew is stopped before the lease expires (run outcome ``lease_lost``).
The Board service is a stub. Design: hermes-mods ``core/kanban-central-service.md``."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def lease_client():
    yield from support.lease_client()


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


@pytest.fixture
def primary(synced_conn):
    sync.set_mode(synced_conn, "primary")
    return synced_conn


def _gateway_id(conn) -> str:
    return conn.execute("SELECT gateway_id FROM kanban_sync_state").fetchone()[0]


def _run_rows(conn, tid):
    return conn.execute("SELECT uid, outcome, status FROM task_runs WHERE task_id = ? ORDER BY id", (tid,)).fetchall()


def test_granted_lease_claims_with_its_run_uid_and_deadline(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="leased", assignee="worker")
    claimed = kb.claim_task(conn, tid, ttl_seconds=900)
    assert claimed is not None and claimed.status == "running"
    [(kind, req)] = lease_client.calls
    assert kind == "acquire" and req["task_uid"] == tid and req["gateway_id"] == _gateway_id(conn)
    assert 890 <= req["ttl_s"] <= 900
    [(run_uid, _outcome, _status)] = _run_rows(conn, tid)
    assert run_uid == req["run_uid"]
    holder, lease_ms, claim_expires = conn.execute(
        "SELECT lease_holder, lease_expires, claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert holder == _gateway_id(conn)
    assert claim_expires <= lease_ms // 1000 - sync.LEASE_SAFETY_MARGIN_SECONDS
    assert conn.execute("SELECT pending_run_uid FROM kanban_sync_state").fetchone()[0] is None


@pytest.mark.parametrize("mode", ["deny", "offline"])
def test_no_lease_leaves_the_card_ready(primary, lease_client, caplog, mode):
    conn = primary
    lease_client.mode = mode
    tid = kb.create_task(conn, title="waits", assignee="worker")
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        assert kb.claim_task(conn, tid) is None
    assert kb.get_task(conn, tid).status == "ready"
    assert _run_rows(conn, tid) == []
    expected = "claim_deferred: offline" if mode == "offline" else "claim_deferred: held"
    assert any(expected in r.getMessage() for r in caplog.records)


def test_no_registered_client_counts_as_offline(primary, caplog):
    sync.set_lease_client(None)
    tid = kb.create_task(primary, title="no client", assignee="worker")
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        assert kb.claim_task(primary, tid) is None
    assert kb.get_task(primary, tid).status == "ready"
    assert any("claim_deferred: offline" in r.getMessage() for r in caplog.records)


def test_review_claim_also_needs_a_lease(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="review me", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    lease_client.mode = "offline"
    assert kb.claim_review_task(conn, tid) is None
    lease_client.mode = "grant"
    assert kb.claim_review_task(conn, tid) is not None
    assert _run_rows(conn, tid)[0][0] == lease_client.calls[-1][1]["run_uid"]


def test_shadow_board_claims_without_asking(synced_conn, lease_client):
    tid = kb.create_task(synced_conn, title="shadow", assignee="worker")
    assert kb.claim_task(synced_conn, tid) is not None
    assert lease_client.calls == []


def test_lease_granted_for_a_lost_cas_is_released(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="already taken", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock = 'elsewhere:1' WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid) is None
    assert [k for k, _ in lease_client.calls] == ["acquire", "release"]
    assert lease_client.calls[1][1]["run_uid"] == lease_client.calls[0][1]["run_uid"]


def test_heartbeat_renews_at_most_once_per_third_of_the_ttl(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="beats", assignee="worker")
    lock = "host:1"
    lease_client.lease_seconds = 900
    kb.claim_task(conn, tid, ttl_seconds=900, claimer=lock)
    assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 0  # lease still fresh
    lease_client.lease_seconds = 400  # pretend 500s of the lease are used up
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int((time.time() + 400) * 1000), tid))
    lease_client.lease_seconds = 900
    assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 1
    lease_ms = conn.execute("SELECT lease_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert lease_ms / 1000 > time.time() + 800


def _spawn_worker():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    reaper = threading.Thread(target=proc.wait, daemon=True)  # reap at once so the pid is really gone
    reaper.start()
    return proc, reaper


def _attach_worker(conn, tid, proc):
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                     (proc.pid, kbd._process_fingerprint(proc.pid), int(time.time()), tid))


def test_worker_that_cannot_renew_is_stopped_before_lease_expiry(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="network drops", assignee="worker")
    lease_client.lease_seconds = 30  # lease already inside the 60s safety margin
    kb.claim_task(conn, tid, ttl_seconds=900)
    lease_ms = conn.execute("SELECT lease_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    proc, reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        lease_client.renew_mode = "offline"
        kb.heartbeat_claim(conn, tid, ttl_seconds=900)
        assert lease_client.count("renew") == 1
        claim_expires = conn.execute("SELECT claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        assert claim_expires <= lease_ms // 1000 - sync.LEASE_SAFETY_MARGIN_SECONDS
        assert kb.release_stale_claims(conn) == 1
        stopped_at_ms = time.time() * 1000
        reaper.join(timeout=30)
        assert proc.poll() is not None, "worker kept running after its lease was lost"
        assert stopped_at_ms < lease_ms
        assert kb.get_task(conn, tid).status == "ready"
        assert _run_rows(conn, tid)[-1][1:] == ("lease_lost", "lease_lost")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_live_worker_with_a_valid_lease_keeps_its_claim(primary, lease_client):
    """Control for the test above: same stale claim, lease still valid -> extended."""
    conn = primary
    tid = kb.create_task(conn, title="healthy", assignee="worker")
    kb.claim_task(conn, tid, ttl_seconds=900)
    proc, _reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, tid))
        assert kb.release_stale_claims(conn) == 0
        assert proc.poll() is None
        assert kb.get_task(conn, tid).status == "running"
    finally:
        proc.kill()


def test_lease_moved_to_another_gateway_stops_the_local_worker(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="reset by operator", assignee="worker")
    kb.claim_task(conn, tid, ttl_seconds=900)
    proc, reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        far = int((time.time() + 3600) * 1000)
        sync.apply_remote_changes(conn, [sync.RemoteChange(
            1, "tasks", tid, "lease", {"lease_holder": "gw_other", "lease_expires": far}, 9, "", "gw_other",
        )], new_cursor=1)
        assert kb.release_stale_claims(conn) == 1
        reaper.join(timeout=30)
        assert proc.poll() is not None
        assert _run_rows(conn, tid)[-1][1] == "lease_lost"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_operator_reclaim_releases_the_lease(primary, lease_client):
    conn = primary
    tid = kb.create_task(conn, title="reclaimed", assignee="worker")
    kb.claim_task(conn, tid)
    run_uid = lease_client.calls[0][1]["run_uid"]
    assert kb.reclaim_task(conn, tid, reason="operator")
    assert lease_client.calls[-1][0] == "release" and lease_client.calls[-1][1]["run_uid"] == run_uid
