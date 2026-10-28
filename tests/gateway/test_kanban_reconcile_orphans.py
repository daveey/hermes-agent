"""Tests: orphaned-card reconciliation for the kanban dispatcher.

Tracked-state vs. reality divergence: a task can sit in ``status='running'``
with broken claim bookkeeping — ``claim_lock IS NULL`` or ``claim_expires IS
NULL`` (crash mid-claim, manual SQL, DB restore, partial migration). None of
the existing recovery paths ever touch such a card:

- ``release_stale_claims`` requires ``claim_expires IS NOT NULL``;
- ``detect_crashed_workers`` requires a host-local ``claim_lock`` prefix and
  a recorded ``worker_pid``;
- ``detect_stale_running`` is disabled by default (``stale_timeout=0``).

Result: a zombie card that shows Running forever. ``reconcile_orphaned_running``
is the reconciliation pass: it finds those orphans, requeues them to ``ready``
with an explanatory note, and logs a ``reconciled`` event. Wired into
``dispatch_once`` each tick, gated by ``kanban.reconcile_orphans`` (config.yaml,
default on) at the gateway watcher layer.

Inspired by openai/symphony's tracker reconciliation (Apache-2.0), idea-level.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _orphan_running(conn, tid, *, claim_lock=None, claim_expires=None,
                    worker_pid=None):
    """Force a task into running with (partially) broken claim bookkeeping."""
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
        "worker_pid=? WHERE id=?",
        (claim_lock, claim_expires, worker_pid, tid),
    )
    conn.commit()


def _orphaned_claim(conn, lane, *, claimed_age=None):
    """Claim a card through ``lane`` (``ready`` or ``review``), then drop its claim bookkeeping as a
    dead claimer would. The ``claimed`` event is aged past the in-flight grace unless ``claimed_age``."""
    tid = kb.create_task(conn, title=f"{lane}-orphan", assignee="w")
    if lane == "review":
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        assert kb.claim_review_task(conn, tid)
    else:
        assert kb.claim_task(conn, tid)
    age = 3600 if claimed_age is None else claimed_age  # well past ORPHAN_CLAIM_GRACE_SECONDS
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'claimed'",
                 (int(time.time()) - age, tid))
    conn.commit()
    _orphan_running(conn, tid)
    return tid


class TestReconcileOrphanedRunning:
    def test_null_claim_lock_orphan_requeued(self, conn):
        """running + claim_lock NULL → requeued to ready with a note."""
        tid = kb.create_task(conn, title="zombie", assignee="w")
        _orphan_running(conn, tid)

        reconciled = kbd.reconcile_orphaned_running(conn)

        assert reconciled == [tid]
        row = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid "
            "FROM tasks WHERE id=?", (tid,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        assert row["worker_pid"] is None

    def test_null_claim_expires_orphan_requeued(self, conn):
        """running + claim_lock set but claim_expires NULL is also invisible
        to release_stale_claims — reconciliation must catch it."""
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="half-claim", assignee="w")
        _orphan_running(conn, tid, claim_lock=f"{host}:dead")

        reconciled = kbd.reconcile_orphaned_running(conn)

        assert reconciled == [tid]
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"] == "ready"

    def test_reconciled_event_and_note_logged(self, conn):
        tid = kb.create_task(conn, title="zombie", assignee="w")
        _orphan_running(conn, tid)

        kbd.reconcile_orphaned_running(conn)

        events = kb.list_events(conn, tid)
        recon = [e for e in events if e.kind == "reconciled"]
        assert len(recon) == 1
        assert recon[0].payload["reason"] == "orphaned_running"
        comments = kb.list_comments(conn, tid)
        assert any("reconcil" in (c.body or "").lower() for c in comments)

    def test_healthy_running_task_untouched(self, conn):
        """A properly claimed running task is NOT an orphan."""
        tid = kb.create_task(conn, title="healthy", assignee="w")
        kb.claim_task(conn, tid)

        assert kbd.reconcile_orphaned_running(conn) == []
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"] == "running"

    def test_live_worker_pid_defers_reconcile(self, conn):
        """If the orphan row still records a live PID on this host, don't
        requeue beside a possibly-alive worker — defer to the next tick."""
        tid = kb.create_task(conn, title="maybe-alive", assignee="w")
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            _orphan_running(conn, tid, worker_pid=sleeper.pid)
            assert kbd.reconcile_orphaned_running(conn) == []
            assert conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()["status"] == "running"
        finally:
            sleeper.terminate()
            sleeper.wait()

    def test_dead_worker_pid_orphan_requeued(self, conn):
        """Orphan with a recorded but dead PID is reconciled."""
        tid = kb.create_task(conn, title="dead-pid", assignee="w")
        dead = subprocess.Popen(["true"])
        dead.wait()
        _orphan_running(conn, tid, worker_pid=dead.pid)

        assert kbd.reconcile_orphaned_running(conn) == [tid]

    def test_review_run_orphan_requeued_to_review(self, conn):
        """A reviewer run that lost its claim goes back to ``review``: requeueing it to ``ready``
        spawns an implementation worker on finished work (MacBook t_01a18189ff87b1c1)."""
        tid = _orphaned_claim(conn, "review")

        assert kbd.reconcile_orphaned_running(conn) == [tid]

        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "review"
        recon = [e for e in kb.list_events(conn, tid) if e.kind == "reconciled"]
        assert recon[-1].payload["retry_status"] == "review"
        assert any("requeued to review" in (c.body or "") for c in kb.list_comments(conn, tid))

    def test_implementation_run_orphan_still_requeued_to_ready(self, conn):
        tid = _orphaned_claim(conn, "ready")

        assert kbd.reconcile_orphaned_running(conn) == [tid]

        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "ready"
        assert any("requeued to ready" in (c.body or "") for c in kb.list_comments(conn, tid))

    def test_card_claimed_within_grace_is_not_reconciled(self, conn):
        """A ``claimed`` event younger than the claim-publication grace means a claim is in flight
        (the Board's grant row can land before, or instead of, the local claim): leave it."""
        tid = _orphaned_claim(conn, "review", claimed_age=5)

        assert kbd.reconcile_orphaned_running(conn) == []
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "running"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "reconciled"]

    def test_non_running_statuses_ignored(self, conn):
        for status in ("todo", "ready", "blocked", "done"):
            tid = kb.create_task(conn, title=f"s-{status}", assignee="w")
            conn.execute(
                "UPDATE tasks SET status=?, claim_lock=NULL, "
                "claim_expires=NULL WHERE id=?", (status, tid),
            )
        conn.commit()
        assert kbd.reconcile_orphaned_running(conn) == []


class TestDispatchOnceReconciles:
    def test_dispatch_once_reconciles_orphans(self, conn):
        tid = kb.create_task(conn, title="zombie", assignee="w")
        _orphan_running(conn, tid)

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (True, ""),
                                  dry_run=True)

        assert tid in result.reconciled_orphans
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"] == "ready"

    def test_dispatch_once_reconcile_can_be_disabled(self, conn):
        """kanban.reconcile_orphans=false plumbs through as
        reconcile_orphans=False and skips the pass."""
        tid = kb.create_task(conn, title="zombie", assignee="w")
        _orphan_running(conn, tid)

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (True, ""),
                                  dry_run=True, reconcile_orphans=False)

        assert result.reconciled_orphans == []
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"] == "running"
