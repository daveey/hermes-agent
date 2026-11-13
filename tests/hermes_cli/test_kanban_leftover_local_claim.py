"""A card that is not ``running`` holds no local claim (self-heal agent-dc4177df2e). [daveey]

T6 (t_01a1a09e94bfaa25) went running -> ready through a Board lease-expiry row from the sync feed. Claim fields are
local-only and never overwritten by a pull, so ``claim_lock``/``worker_pid`` of the dead worker stayed set. The
dispatcher only claims ready cards with ``claim_lock IS NULL`` and both cleanup sweeps look at ``running`` only, so
the card sat ready for 5 days and its read said nothing about why.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_queue_position as kqp

DEAD_PID = 2 ** 22 - 3  # above the default pid_max: never a live process


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _feed_requeued(conn, *, pid=DEAD_PID, lock=None, status="ready"):
    """A card claimed here, then moved off ``running`` by a status-only write (the feed's lease-expiry row): local
    claim fields stay as the dead claimer left them. The claim is aged past the in-flight grace."""
    tid = kb.create_task(conn, title="t6", assignee="w")
    assert kb.claim_task(conn, tid)
    lock = lock or f"{kb._host_prefix()}28820"
    conn.execute("UPDATE tasks SET status = ?, claim_lock = ?, worker_pid = ?, worker_started_at = NULL "
                 "WHERE id = ?", (status, lock, pid, tid))
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'claimed'",
                 (int(time.time()) - 3600, tid))
    conn.commit()
    return tid


def _claim_cols(conn, tid):
    return tuple(conn.execute(
        "SELECT claim_lock, claim_expires, worker_pid, worker_started_at, last_heartbeat_at FROM tasks WHERE id = ?",
        (tid,)).fetchone())


def test_ready_card_with_dead_claim_is_released_then_claimable(conn):
    tid = _feed_requeued(conn)
    assert kb.claim_task(conn, tid) is None  # the bug: never dispatchable

    assert kbd.release_leftover_local_claims(conn) == [tid]

    assert _claim_cols(conn, tid) == (None, None, None, None, None)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "ready"
    released = [e for e in kb.list_events(conn, tid) if e.kind == "claim_released"]
    assert released and released[-1].payload["reason"] == "leftover_local_claim"
    assert released[-1].payload["claim_lock"].endswith(":28820")
    assert kb.claim_task(conn, tid) is not None


def test_dispatch_tick_releases_it_with_nothing_running(conn):
    """The sweep runs every tick, also when no card is running (T6's board had none running at times)."""
    tid = _feed_requeued(conn)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (True, ""), dry_run=True)
    assert _claim_cols(conn, tid)[0] is None


@pytest.mark.parametrize("status", ["todo", "review", "blocked", "scheduled", "done", "triage"])
def test_every_non_running_status_is_covered(conn, status):
    tid = _feed_requeued(conn, status=status)
    assert kbd.release_leftover_local_claims(conn) == [tid]
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == status


def test_live_worker_keeps_its_claim(conn):
    import os
    tid = _feed_requeued(conn, pid=os.getpid())
    assert kbd.release_leftover_local_claims(conn) == []
    assert _claim_cols(conn, tid)[0] is not None


def test_other_hosts_lock_is_not_judged_by_a_local_pid(conn):
    tid = _feed_requeued(conn, lock="some-other-host:28820")
    assert kbd.release_leftover_local_claims(conn) == []
    assert _claim_cols(conn, tid)[0] == "some-other-host:28820"


def test_claim_in_flight_is_left_alone(conn):
    tid = _feed_requeued(conn)
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'claimed'",
                 (int(time.time()), tid))
    conn.commit()
    assert kbd.release_leftover_local_claims(conn) == []


def test_running_card_is_not_touched(conn):
    tid = _feed_requeued(conn, status="running")
    assert kbd.release_leftover_local_claims(conn) == []
    assert _claim_cols(conn, tid)[0] is not None


def test_card_read_names_the_leftover_claim(conn):
    """Detection: the read of such a card says why it waits instead of a local-slot wait or nothing."""
    tid = _feed_requeued(conn)
    place = kqp.read(conn, kb.get_task(conn, tid))
    assert place["lane"] == "leftover_claim"
    assert place["claim_lock"].endswith(":28820")
    note = kqp.describe(place)
    assert "leftover claim" in note and "worker gone" in note
