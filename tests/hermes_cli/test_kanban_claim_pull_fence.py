"""A pull never moves a just-claimed card off the run this gateway claimed until the Board acknowledged the claim,
and the dispatcher records the spawn under the run it claimed. [daveey]

The live failure (MacBook ``default``, t_01a189343dd2f6ac run 9821, 2026-10-29 13:44:21 PDT): the previous run's
end (``changes_requested``) was pushed and acknowledged (``sync_version`` 35); its echo (Board version 35: ``ready``,
no current run) still waited in the feed. The sync round fetched that page and read the pending outbox fields
(empty) while the dispatcher's claim transaction was in flight; the apply then waited on the claim's write lock and
ran after it. The version fence lets an equal version through (``a re-send``), so the echo set the card back to
``ready`` with ``current_run_id`` NULL. The spawn a second later re-read ``current_run_id`` (NULL): the ``spawned``
event had no run id and the run row got no pid. The worker's startup identity scrub then saw no live run and dropped
``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID``, so it never heartbeated (21 min).

Service: the in-memory Board fake with Board-faithful lease runs (``kanban_sync_fake_board``).
"""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_identity as kid
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        conn.close()


def _round(kanban_home, svc):
    res = ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert res.error is None and (res.push is None or res.push.rejected == 0)
    return res


def _card(conn, tid):
    return conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()


def _acked_card_with_its_echo_waiting(live, kanban_home, svc) -> str:
    """A card whose last local write was pushed and acknowledged, with that write's echo still in the feed (the
    previous run's ``changes_requested``)."""
    tid = kb.create_task(live, title="pull fence", assignee="worker")
    _round(kanban_home, svc)  # the card reaches the Board
    _round(kanban_home, svc)  # its echo comes back: everything settled
    live.execute("UPDATE tasks SET priority = 3 WHERE id = ?", (tid,))
    live.commit()
    _round(kanban_home, svc)  # pushed and acknowledged; the echo now waits in the feed
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'tasks'").fetchone()[0] == 0
    assert _card(live, tid)["status"] == "ready"
    return tid


def _claim_inside_the_pull(monkeypatch, live, tid) -> dict:
    """Commit the claim after the live pull read its pending fields and fetched its page, before its apply."""
    claimed: dict = {}
    original = ksc.pending_task_fields

    def racing(conn):
        pending = original(conn)
        is_live = conn.execute("PRAGMA database_list").fetchone()[2].endswith("kanban.db")
        if is_live and not claimed:
            task = kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
            assert task is not None and task.current_run_id is not None
            claimed["run_id"] = task.current_run_id
        return pending

    monkeypatch.setattr(ksc, "pending_task_fields", racing)
    return claimed


def test_a_pull_racing_the_claim_keeps_the_claimed_run(live, kanban_home, svc, monkeypatch):
    tid = _acked_card_with_its_echo_waiting(live, kanban_home, svc)
    claimed = _claim_inside_the_pull(monkeypatch, live, tid)
    _round(kanban_home, svc)  # pull (the pre-claim echo) racing the claim, then push the claim
    run_id = claimed["run_id"]
    card = _card(live, tid)
    assert (card["status"], card["current_run_id"]) == ("running", run_id), dict(card)
    for _ in range(3):  # (the race fires once) the Board's grant rows, the claim's push and its echo all keep it
        _round(kanban_home, svc)
        card = _card(live, tid)
        assert (card["status"], card["current_run_id"]) == ("running", run_id), dict(card)
    uid = live.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    board = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (board["status"], board["current_run_uid"]) == ("running", uid)


class _SnapshotOf:
    """A Board snapshot frozen at one moment: ``resync`` pages through these rows."""

    def __init__(self, svc, head):
        self.rows = copy.deepcopy(svc.board(BOARD)["rows"])
        self.head = head

    def snapshot_page(self, board, table, after="", limit=1000):
        return {"head": self.head, "rows": list(self.rows.get(table, {}).values()), "more": False}


def test_a_snapshot_rebuild_keeps_an_unacknowledged_claim(live, kanban_home, svc):
    """The snapshot rebuild is a pull too: the Board's copy of the card from before our claim leaves ``status``
    and ``current_run_id`` alone while the claim is in the outbox, and the row's other fields still land."""
    tid = _acked_card_with_its_echo_waiting(live, kanban_home, svc)
    svc.board(BOARD)["rows"]["tasks"][tid]["title"] = "renamed on the Board"
    before_claim = _SnapshotOf(svc, svc.board(BOARD)["seq"])
    task = kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    assert task is not None
    ksc.resync(live, before_claim, BOARD, observers=True, partition=ME)
    card = _card(live, tid)
    assert (card["status"], card["current_run_id"]) == ("running", task.current_run_id), dict(card)
    assert live.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "renamed on the Board"


def test_once_the_board_acknowledged_the_claim_its_rows_apply_again(live, kanban_home, svc):
    """The fence lasts only while the claim is unacknowledged: after the push, a newer Board write of the
    card's status lands as before."""
    tid = _acked_card_with_its_echo_waiting(live, kanban_home, svc)
    task = kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    assert task is not None
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ?",
                        (tid,)).fetchone()[0] == 0
    version = live.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    newer = sync.RemoteChange(0, "tasks", tid, "upsert", {"status": "blocked"}, version + 1,
                              f"{version + 1:013d}-0000-server", "gw_other", None)
    sync.apply_remote_changes(live, [newer], new_cursor=sync.sync_state(live)["cursor"], board=BOARD)
    assert _card(live, tid)["status"] == "blocked"


def test_a_worker_started_in_the_race_window_keeps_its_identity(live, kanban_home, svc, monkeypatch):
    tid = _acked_card_with_its_echo_waiting(live, kanban_home, svc)
    claimed = _claim_inside_the_pull(monkeypatch, live, tid)
    _round(kanban_home, svc)
    env = {"HERMES_KANBAN_DB": str(kanban_home / "kanban.db"), kid.TASK_KEY: tid,
           kid.RUN_KEY: str(claimed["run_id"])}
    assert kid.drop_finished_kanban_identity(env) == []
    assert env[kid.TASK_KEY] == tid and env[kid.RUN_KEY] == str(claimed["run_id"])


# --- dispatch: the spawn is recorded under the claimed run --------------------------------------------------------


@pytest.fixture
def plain_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    profile = home / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_managed_yaml())
    assert _assignee_config("alice")["model_routing"]["enabled"]
    kb.init_db()
    return home


def test_spawn_records_the_claimed_run_even_if_current_run_id_reads_null(plain_home, all_assignees_spawnable):
    """Whatever blanks ``current_run_id`` mid-spawn, the ``spawned`` event and the run row carry the run the
    dispatcher claimed (the one it put in ``HERMES_KANBAN_RUN_ID``) and the worker pid."""
    pid = os.getpid()
    seen: dict = {}

    def spawn(task, workspace, board=None):
        seen["run_id"] = task.current_run_id
        with kbc.connect() as other:
            other.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task.id,))
            other.commit()
        return pid

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spawn", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert [t for t, _a, _w in res.spawned] == [tid]
        run_id = seen["run_id"]
        assert run_id is not None
        [event] = [e for e in kb.list_events(conn, tid) if e.kind == "spawned"]
        assert event.run_id == run_id
        assert event.payload["pid"] == pid
        row = conn.execute("SELECT worker_pid FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["worker_pid"] == pid
        assert conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == pid


def test_spawn_after_a_reclaim_does_not_write_the_pid_onto_a_newer_run(plain_home, all_assignees_spawnable):
    """The claimed run id, not ``current_run_id``, picks the run row: a card that moved on to a newer run
    mid-spawn keeps that newer run's row untouched."""
    pid = os.getpid()
    seen: dict = {}

    def spawn(task, workspace, board=None):
        seen["run_id"] = task.current_run_id
        with kbc.connect() as other:
            newer = other.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'alice', 'running', ?)",
                (task.id, int(time.time()))).lastrowid
            other.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (newer, task.id))
            other.commit()
            seen["newer"] = newer
        return pid

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spawn", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id = ?", (seen["run_id"],)).fetchone()[0] == pid
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id = ?", (seen["newer"],)).fetchone()[0] is None
        [event] = [e for e in kb.list_events(conn, tid) if e.kind == "spawned"]
        assert event.run_id == seen["run_id"]
