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


def _lapse(conn, tid, seconds_ago: int) -> None:
    """The lease and the claim ran out ``seconds_ago`` on the wall clock with no renewal: the machine slept (or the
    backend restarted) while the worker was alive, so nothing renewed in between."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ?, claim_expires = ? WHERE id = ?",
                     (int((time.time() - seconds_ago) * 1000), int(time.time()) - 1, tid))


def test_lease_gap_does_not_kill_a_live_worker_when_the_board_still_holds_it(primary, lease_client):
    """Regression (card t_01a18628253a543a): the Mac slept 17:59-18:14 PDT with live workers; on the first wake the
    dispatcher saw their leases past expiry on its own clock and SIGTERMed them. The Board holds a dark holder's
    lease, so the dispatcher must renew it and keep the worker."""
    conn = primary
    tid = kb.create_task(conn, title="slept through renewals", assignee="worker")
    kb.claim_task(conn, tid, ttl_seconds=900)
    run_uid = lease_client.calls[0][1]["run_uid"]
    proc, _reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        _lapse(conn, tid, 15 * 60)
        assert kb.release_stale_claims(conn) == 0
        assert proc.poll() is None, "a live worker was stopped for a lease the Board still held"
        assert kb.get_task(conn, tid).status == "running"
        assert lease_client.calls[-1][0] == "renew" and lease_client.calls[-1][1]["run_uid"] == run_uid
        lease_ms, claim_expires = conn.execute(
            "SELECT lease_expires, claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert lease_ms / 1000 > time.time() + 800 and claim_expires > time.time()
        assert _run_rows(conn, tid)[-1][1:] == (None, "running")
    finally:
        proc.kill()


def test_lease_gap_with_the_board_unreachable_keeps_the_worker(primary, lease_client):
    """A maintenance wake has no network (agent.log 18:18:47: Errno 51), so the lease cannot be checked: keep the
    worker and ask again, instead of stopping it on our clock alone."""
    conn = primary
    tid = kb.create_task(conn, title="dark wake", assignee="worker")
    kb.claim_task(conn, tid, ttl_seconds=900)
    proc, _reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        _lapse(conn, tid, 20 * 60)
        lease_client.renew_mode = "offline"
        assert kb.release_stale_claims(conn) == 0
        assert proc.poll() is None
        assert kb.get_task(conn, tid).status == "running"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
        assert kinds[-1] == "claim_extended" and "reclaimed" not in kinds
        # Back online: the next sweep renews it.
        lease_client.renew_mode = "grant"
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, tid))
        assert kb.release_stale_claims(conn) == 0
        assert proc.poll() is None
        assert conn.execute("SELECT lease_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0] > time.time() * 1000
    finally:
        proc.kill()


@pytest.mark.parametrize("case", ["refused", "offline_past_dark_hold"])
def test_lease_gap_stops_the_worker_once_the_lease_is_really_gone(primary, lease_client, case):
    """Controls: the Board refuses the renewal (it gave the card away), or it stayed unreachable past the hold."""
    conn = primary
    tid = kb.create_task(conn, title="really gone", assignee="worker")
    kb.claim_task(conn, tid, ttl_seconds=900)
    proc, reaper = _spawn_worker()
    try:
        _attach_worker(conn, tid, proc)
        if case == "refused":
            _lapse(conn, tid, 15 * 60)
            lease_client.renew_mode = "deny"
        else:
            _lapse(conn, tid, sync.LEASE_DARK_HOLD_SECONDS + 60)
            lease_client.renew_mode = "offline"
        assert kb.release_stale_claims(conn) == 1
        reaper.join(timeout=30)
        assert proc.poll() is not None, "worker kept running after its lease was gone"
        assert kb.get_task(conn, tid).status == "ready"
        assert _run_rows(conn, tid)[-1][1:] == ("lease_lost", "lease_lost")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_first_heartbeat_after_sleep_renews_a_lapsed_lease(primary, lease_client):
    """The renewal throttle runs on the monotonic clock, which stops during macOS sleep: a renewal attempted just
    before the lid closed must not block the first renewal after wake, when the lease has already lapsed."""
    conn = primary
    tid = kb.create_task(conn, title="wakes up", assignee="worker")
    lock = "host:1"
    kb.claim_task(conn, tid, ttl_seconds=900, claimer=lock)
    sync._RENEW_ATTEMPTS[tid] = time.monotonic() - 60  # last attempt 1 min "ago" (monotonic) = before the sleep
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int((time.time() - 600) * 1000), tid))
    assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 1
    assert conn.execute("SELECT claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0] > time.time() + 600


def test_heartbeat_with_no_current_run_makes_no_renewal_call(primary, lease_client, caplog):
    """A heartbeat that lands after the run ended (continued/blocked, ``current_run_id`` NULL) has no run uid to
    renew under; the Board refuses an empty one (hub 400 "run_uid required"), so no call and no warning."""
    conn = primary
    tid = kb.create_task(conn, title="run ended", assignee="worker")
    lock = "host:1"
    kb.claim_task(conn, tid, ttl_seconds=900, claimer=lock)
    lease_ms = int((time.time() + 200) * 1000)  # a third of the lease used up: a renewal would be due
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET current_run_id = NULL, lease_expires = ? WHERE id = ?", (lease_ms, tid))
    lease_client.renew_mode = "offline"  # what the hub's 400 surfaces as, should a call slip through
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    claim_expires = conn.execute("SELECT claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert claim_expires <= lease_ms // 1000 - sync.LEASE_SAFETY_MARGIN_SECONDS  # still bounded by the lease


def _renewal_logs(caplog):
    return [r.getMessage() for r in caplog.records if "renewed" in r.getMessage() and "lapsed" in r.getMessage()]


def test_renewal_of_a_lapsed_lease_logs_how_long_it_had_lapsed(primary, lease_client, caplog):
    conn = primary
    tid = kb.create_task(conn, title="slept", assignee="worker")
    lock = "host:1"
    kb.claim_task(conn, tid, ttl_seconds=900, claimer=lock)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int((time.time() - 338) * 1000), tid))
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 1
    [msg] = _renewal_logs(caplog)
    assert msg in (f"kanban sync: lease of {tid} renewed {n}s after it lapsed" for n in (338, 339))
    [rec] = [r for r in caplog.records if r.getMessage() == msg]
    assert rec.levelno == logging.INFO


def test_renewal_inside_the_normal_window_logs_nothing(primary, lease_client, caplog):
    conn = primary
    tid = kb.create_task(conn, title="on time", assignee="worker")
    lock = "host:1"
    kb.claim_task(conn, tid, ttl_seconds=900, claimer=lock)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int((time.time() + 400) * 1000), tid))
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_db_sync"):
        assert kb.heartbeat_claim(conn, tid, ttl_seconds=900, claimer=lock)
    assert lease_client.count("renew") == 1
    assert [r.getMessage() for r in caplog.records if r.name == "hermes_cli.kanban_db_sync"] == []


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


def test_reclaim_of_a_foreign_live_lease_does_not_end_the_run(primary, lease_client):
    """A replica must not close a run another gateway still holds.

    David-MBP 2026-10-28 23:05: reclaim of hub-leased cards (claim_lock NULL here)
    ended the run; the Board refused the status change; the hub reaper SIGTERMed
    the live workers ~3 min later.
    """
    conn = primary
    tid = kb.create_task(conn, title="hub-owned", assignee="worker")
    kb.claim_task(conn, tid)
    run_uid = lease_client.calls[0][1]["run_uid"]
    far = int((time.time() + 3600) * 1000)
    conn.execute(
        "UPDATE tasks SET lease_holder = ?, lease_expires = ?, claim_lock = NULL, worker_pid = NULL WHERE id = ?",
        ("hub", far, tid),
    )
    before = conn.execute(
        "SELECT status, ended_at FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchall()
    assert kb.reclaim_task(conn, tid, reason="no local worker") is False
    assert kb.get_task(conn, tid).status == "running"
    after = conn.execute(
        "SELECT status, ended_at FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchall()
    assert after == before
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
    assert "reclaim_refused" in kinds
    assert "reclaimed" not in kinds
    assert not any(kind == "release" and req.get("run_uid") == run_uid for kind, req in lease_client.calls)
