"""The feed that applies this host's queued failure release also clears the local claim. [daveey]

worker-7 t_01a1d62545843356: run 2440 (rc=0 protocol violation) went ``ready`` through the Board feed but kept
``claim_lock``/``worker_pid`` until the leftover sweep's 180 s grace; run 2462 gave up to ``triage`` and kept its
claim forever (``triage`` was not a leftover status), so the creator's archive was refused "it is claimed".
"""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_failure_transport import _running

DEAD_PID = 2 ** 22 - 3


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    from hermes_cli import kanban_sync_client as client
    client.ensure_identity(conn, "default", "fixture")
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


def _claim(conn, tid):
    return tuple(conn.execute("SELECT claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
                              "WHERE id = ?", (tid,)).fetchone())


def _crash_rc0(conn, monkeypatch, pid=DEAD_PID):
    """A primary card whose worker exited rc=0 with no terminal kanban call: the real sweep queues the release."""
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = _running(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, claim_lock=?, claim_expires=?, started_at=1 WHERE id=?",
                     (pid, kb._host_prefix() + "2256394", 4102444800, tid))
        conn.execute("DELETE FROM kanban_sync_outbox")
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 0)
    monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
    monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
    monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)

    def dead_worker(*args, **kwargs):
        dead = dispatch._DeadWorker("crashed", 0, "worker exited cleanly (rc=0) - protocol violation", "crashed",
                                    {"exit_code": 0, "protocol_violation": True})
        dead.protocol_violation = True
        return dead
    monkeypatch.setattr(dispatch, "_classify_dead_worker", dead_worker)
    assert dispatch.detect_crashed_workers(conn) == [tid]
    assert conn.execute("SELECT op FROM kanban_sync_outbox").fetchone()[0] == "release"
    assert _claim(conn, tid)[0] is not None  # the sweep itself leaves the primary mirror alone
    return tid, rid


def _feed(conn, tid, status, version=6):
    change = sync.RemoteChange(seq=version, table="tasks", uid=tid, op="upsert", version=version, hlc="",
                               origin_gateway="board", fields={"status": status})
    sync.apply_remote_changes(conn, [change], new_cursor=version, observers=False)


@pytest.mark.parametrize("status", ["ready", "triage", "blocked"])
def test_feed_release_clears_claim_in_the_same_apply(live, monkeypatch, status):
    """ready = the rc=0 retry; triage/blocked = the give-up. No grace wait, no leftover sweep."""
    tid, _ = _crash_rc0(live, monkeypatch)
    _feed(live, tid, status)
    assert live.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == status
    assert _claim(live, tid) == (None, None, None, None)


def test_ready_retry_is_claimable_at_once(live, monkeypatch):
    tid, _ = _crash_rc0(live, monkeypatch)
    _feed(live, tid, "ready")
    row = live.execute("SELECT claim_lock FROM tasks WHERE id=? AND status='ready'", (tid,)).fetchone()
    assert row is not None and row[0] is None


def test_give_up_to_triage_can_be_archived(live, monkeypatch):
    tid, _ = _crash_rc0(live, monkeypatch)
    _feed(live, tid, "triage")
    from hermes_cli import kanban_archive_scope as scope
    assert scope._worker_may_archive_own(live, kb.get_task(live, tid)) is None


def test_status_move_without_our_release_keeps_the_claim(live, monkeypatch):
    """A lease-expiry status row with no queued command is the leftover sweep's job (grace applies)."""
    tid, _ = _running(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=?, claim_lock=? WHERE id=?",
                     (DEAD_PID, kb._host_prefix() + "1", tid))
    _feed(live, tid, "ready")
    assert _claim(live, tid)[0] == kb._host_prefix() + "1"


def test_live_worker_keeps_the_claim(live, monkeypatch):
    tid, _ = _crash_rc0(live, monkeypatch, pid=os.getpid())
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: True)
    _feed(live, tid, "ready")
    assert _claim(live, tid)[2] == os.getpid()


def test_still_running_on_the_feed_keeps_the_claim(live, monkeypatch):
    tid, _ = _crash_rc0(live, monkeypatch)
    _feed(live, tid, "running")
    assert _claim(live, tid)[0] is not None
