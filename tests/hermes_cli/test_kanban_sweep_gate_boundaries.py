"""Quiet-board process and lookback boundaries (post-merge review of #2677)."""
from __future__ import annotations

import subprocess
import sys
import time
from unittest import mock

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_timed_park as ktp
from hermes_cli.kanban_idle_candidates import sweep_shared_blockers
from tests.hermes_cli import test_kanban_sweep_gate as helpers

home = helpers.home


def test_non_running_local_worker_death_releases_claim_on_quiet_board(home, monkeypatch):
    # A status-only feed update preserves host-local claim fields. The orphan
    # grace has elapsed before the observation window; no synthetic PID probes.
    clock = [int(time.time())]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="feed moved worker to ready", assignee="nobody")
            assert kb.claim_task(conn, tid)
            kbd._set_worker_pid(conn, tid, proc.pid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            clock[0] += 181
            for _ in range(2):
                helpers._tick(conn)
                task = kb.get_task(conn, tid)
                assert task.status == "ready"
                assert task.claim_lock is not None
                assert task.worker_pid == proc.pid
            before = helpers._revision(conn)
            proc.kill()
            proc.wait(timeout=10)
            assert helpers._revision(conn) == before
            result = helpers._tick(conn)
            assert tid in result.released_leftover_claims
            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.claim_lock is None
            assert task.worker_pid is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()


def test_old_unscoped_assertion_expiry_elects_scoped_probe_without_writes(home, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        old = kb.create_task(conn, title="other provider queued", assignee="nobody")
        assert kb.claim_task(conn, old)
        with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):
            assert kb.block_task(conn, old, reason="wall", kind="capability", blocker_id="wall-x")
        asserted_at = clock[0]
        assert kb.unblock_task(conn, old)
        clock[0] += 60
        candidate = kb.create_task(conn, title="matching provider blocked", assignee="nobody")
        assert kb.claim_task(conn, candidate)
        with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):
            assert kb.block_task(conn, candidate, reason="wall", kind="capability",
                                 blocker_id="wall-x", blocker_provider="anthropic")
        # Routing is input, not the state derivation or the probe election under test.
        monkeypatch.setattr(ksb, "task_route", lambda c, tid: (
            None, "openai" if tid == old else "anthropic", "ready"))
        boundary = asserted_at + 14 * 24 * 60 * 60
        before = helpers._revision(conn)
        for now in (boundary - 1, boundary):
            clock[0] = now
            result = kbd.DispatchResult()
            sweep_shared_blockers(conn, result)
            assert not result.shared_blocker_probed
            assert kb.get_task(conn, old).status == "ready"
            assert kb.get_task(conn, candidate).status == "blocked"
            assert helpers._revision(conn) == before
        clock[0] = boundary + 1
        assert helpers._revision(conn) == before
        result = kbd.DispatchResult()
        sweep_shared_blockers(conn, result)
        assert result.shared_blocker_probed == [candidate]
        assert kb.get_task(conn, candidate).status == "ready"
        assert kb.get_task(conn, old).status == "ready"
