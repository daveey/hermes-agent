"""Sleep-aware stale checks: time the Mac spent suspended is not counted as worker idleness. [daveey]

The dispatcher records a gap between two of its passes that is far longer than its interval as a
suspend (``kanban_suspend_ledger``). The heartbeat backstop of ``release_stale_claims``,
``detect_stale_running`` and ``enforce_max_runtime`` then measure awake time only, so a lid-close
longer than an hour no longer SIGTERMs live workers on the first pass after wake.
"""

import os
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_suspend_ledger as ledger


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        # The fingerprint needs the process to exist; give exec a moment.
        for _ in range(50):
            if kbd._process_fingerprint(proc.pid):
                break
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _running(conn, pid, *, hb_age, started_age=None, max_runtime=None) -> str:
    """A host-local running card with a live worker ``pid``, an expired claim and a heartbeat
    ``hb_age`` seconds old."""
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    now = int(time.time())
    started = now - (started_age if started_age is not None else hb_age)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ?, "
            "last_heartbeat_at = ? WHERE id = ?",
            (kbd._process_fingerprint(pid), started, now - 60, now - hb_age, tid))
        conn.execute("UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? "
                     "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (started, now - hb_age, tid))
    return tid


def _suspend(conn, start, end):
    """Record a suspend the way the dispatcher does: a pass at ``start``, the next at ``end``."""
    ledger.record_dispatch_pass(conn, now=start, interval_seconds=60)
    ledger.record_dispatch_pass(conn, now=end, interval_seconds=60)


def _reaping_kill(proc, killed):
    """signal_fn that really signals the child and reaps it, so the sweep sees it gone."""
    def kill(pid, sig):
        killed.append((pid, sig))
        os.kill(pid, sig)
        proc.wait(timeout=10)
    return kill


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_live_worker_with_recorded_suspend_keeps_claim(board, child):
    now = int(time.time())
    tid = _running(board, child.pid, hb_age=2 * 3600)
    _suspend(board, now - 2 * 3600 + 60, now - 30)

    killed = []
    assert kb.release_stale_claims(board, signal_fn=lambda p, s: killed.append(p)) == 0
    assert killed == []
    kinds = [e.kind for e in kb.list_events(board, tid)]
    assert "reclaimed" not in kinds
    assert "claim_extended" in kinds
    task = kb.get_task(board, tid)
    assert task.status == "running" and task.claim_expires > now
    assert child.poll() is None and _alive(child.pid)


def test_live_worker_without_suspend_is_reclaimed_heartbeat_stale(board, child):
    tid = _running(board, child.pid, hb_age=2 * 3600)
    killed = []

    assert kb.release_stale_claims(board, signal_fn=_reaping_kill(child, killed)) == 1
    ev = [e for e in kb.list_events(board, tid) if e.kind == "reclaimed"]
    assert ev and ev[-1].payload["heartbeat_stale"] is True
    assert killed and killed[0][0] == child.pid


def test_suspend_ended_61_min_ago_without_heartbeat_is_reclaimed(board, child):
    now = int(time.time())
    # Heartbeat right before a 1-h sleep; awake 61 min since wake with no heartbeat.
    tid = _running(board, child.pid, hb_age=3600 + 61 * 60)
    _suspend(board, now - 3600 - 61 * 60, now - 61 * 60)

    killed = []
    assert kb.release_stale_claims(board, signal_fn=_reaping_kill(child, killed)) == 1
    ev = [e for e in kb.list_events(board, tid) if e.kind == "reclaimed"]
    assert ev and ev[-1].payload["heartbeat_stale"] is True


def test_ledger_records_gap_only_above_threshold_and_fresh_connection_reads_it(board, tmp_path):
    now = int(time.time())
    assert ledger.gap_threshold_seconds(60) == 300
    assert ledger.gap_threshold_seconds(200) == 600

    ledger.record_dispatch_pass(board, now=now - 10_000, interval_seconds=60)
    ledger.record_dispatch_pass(board, now=now - 10_000 + 300, interval_seconds=60)  # == threshold: awake
    ledger.record_dispatch_pass(board, now=now - 5000, interval_seconds=60)  # gap 4700 s: suspended
    ledger.record_dispatch_pass(board, now=now - 4940, interval_seconds=60)  # normal tick

    fresh = kbc.connect(tmp_path / "kanban.db")
    try:
        assert ledger.suspended_intervals(fresh) == [(now - 10_000 + 300, now - 5000)]
        assert ledger.awake_seconds(fresh, now - 10_000, now) == 10_000 - 4700
        # Partial overlap is clipped to the window.
        assert ledger.awake_seconds(fresh, now - 6000, now) == 6000 - 1000
        assert ledger.awake_seconds(fresh, now - 100, now) == 100
    finally:
        fresh.close()


def test_ledger_prunes_intervals_older_than_seven_days(board):
    now = int(time.time())
    _suspend(board, now - 9 * 86400, now - 8 * 86400)
    ledger.record_dispatch_pass(board, now=now - 8 * 86400 + 60, interval_seconds=60)
    assert ledger.suspended_intervals(board) == [(now - 9 * 86400, now - 8 * 86400)]
    ledger.record_dispatch_pass(board, now=now, interval_seconds=60)  # also a gap: ~8 d
    assert ledger.suspended_intervals(board) == [(now - 8 * 86400 + 60, now)]


def test_awake_seconds_without_ledger_table_is_wall_clock(board):
    assert ledger.awake_seconds(board, 1000, 5000) == 4000


def test_max_runtime_skips_suspended_time(board, child):
    now = int(time.time())
    tid = _running(board, child.pid, hb_age=60, started_age=2 * 3600, max_runtime=3600)
    _suspend(board, now - 5400 - 600, now - 600)  # 90-min suspend: 30 min awake

    killed = []
    assert kbd.enforce_max_runtime(board, signal_fn=lambda p, s: killed.append(p)) == []
    assert killed == []
    assert kb.get_task(board, tid).status == "running"
    assert child.poll() is None


def test_max_runtime_without_suspend_times_out(board, child):
    tid = _running(board, child.pid, hb_age=60, started_age=2 * 3600, max_runtime=3600)
    killed = []

    assert kbd.enforce_max_runtime(board, signal_fn=lambda p, s: killed.append(p)) == [tid]
    runs = kb.list_runs(board, tid)
    assert runs[-1].outcome == "timed_out"
    assert killed and killed[0] == child.pid


def test_detect_stale_running_skips_suspended_time(board, child):
    now = int(time.time())
    tid = _running(board, child.pid, hb_age=6 * 3600, started_age=6 * 3600)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (now + 900, tid))
    _suspend(board, now - 6 * 3600 + 120, now - 60)

    killed = []
    assert kbd.detect_stale_running(board, stale_timeout_seconds=4 * 3600,
                                    signal_fn=lambda p, s: killed.append(p)) == []
    assert killed == []
    assert "reclaim_deferred" not in [e.kind for e in kb.list_events(board, tid)]
    assert kb.get_task(board, tid).status == "running"


def test_reclaim_phase_records_gap_before_reclaim(board, child, monkeypatch):
    """End to end: the first dispatcher pass after a 2-h lid-close keeps the live worker."""
    now = int(time.time())
    monkeypatch.setattr(ledger, "_configured_interval_seconds", lambda: 60)
    tid = _running(board, child.pid, hb_age=2 * 3600)
    ledger.record_dispatch_pass(board, now=now - 2 * 3600 + 30, interval_seconds=60)  # pass before sleep

    kbd._run_reclaim_phase(board, kbd.DispatchResult(), stale_timeout_seconds=0,
                           failure_limit=2, reconcile_orphans=False)

    assert ledger.suspended_intervals(board)[-1][0] == now - 2 * 3600 + 30
    assert "reclaimed" not in [e.kind for e in kb.list_events(board, tid)]
    assert kb.get_task(board, tid).status == "running"
    assert child.poll() is None


def test_fresh_run_is_not_stale_against_its_predecessors_heartbeat(board, child):
    """A run claimed 20 s ago inherits the task's 2-h-old heartbeat from its previous run (the card
    was parked). It is judged from its own start: not heartbeat-stale, claim extended, no kill."""
    now = int(time.time())
    tid = _running(board, child.pid, hb_age=2 * 3600, started_age=20)
    killed = []

    assert kb.release_stale_claims(board, signal_fn=lambda p, s: killed.append(p)) == 0
    assert killed == []
    kinds = [e.kind for e in kb.list_events(board, tid)]
    assert "reclaimed" not in kinds and "claim_extended" in kinds
    assert kb.get_task(board, tid).claim_expires > now


def test_dry_run_dispatch_writes_no_stamp_and_keeps_the_gap(board, monkeypatch):
    """A dry run is not a pass: no ledger table, and the next real pass still sees the suspend gap."""
    now = int(time.time())
    monkeypatch.setattr(ledger, "_configured_interval_seconds", lambda: 60)
    before = list(board.iterdump())
    kbd.dispatch_once(board, dry_run=True)
    assert list(board.iterdump()) == before
    assert ledger.record_dispatch_pass(board, now=now - 3600, dry_run=True) is None
    assert list(board.iterdump()) == before

    ledger.record_dispatch_pass(board, now=now - 3600, interval_seconds=60)
    kbd._run_reclaim_phase(board, kbd.DispatchResult(), stale_timeout_seconds=0, failure_limit=2,
                           reconcile_orphans=False, dry_run=True)
    ledger.record_dispatch_pass(board, now=now, interval_seconds=60)
    assert ledger.suspended_intervals(board) == [(now - 3600, now)]
