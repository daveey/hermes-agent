"""The owner's divergence repair must never send a card back to ``running`` over its ended run. [daveey]

Hub incident 2026-11-12 (t_01a1ceaeb6ba593c): worker-4 completed the hub's card (Board v18-v20: ``done``, run
``done/completed``, ``current_run_uid`` NULL). 25 s later the hub's own live file still held the v16 task fields at
the replica's version 20, so ``repair_divergence`` took the live copy for an uncaptured local write and pushed
``status=running``, ``completed_at=NULL``, the ended run as ``current_run_uid`` and the old ``block_kind`` /
``completion_contract`` (Board v21, hub HLC newer than the worker's, so every field won). The same pass did it to
t_01a1ceb44e35f780 (``todo`` after a ``blocked`` run). Both rows then read ``running`` with no lease and an ended run.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _hlc(offset_ms: int, gw: str) -> str:
    return f"{int(time.time() * 1000) + offset_ms:013d}-0000-{gw}"


def _service_task(svc, tid):
    return svc.board(BOARD)["rows"]["tasks"][tid]


def _running_card(live, kanban_home, svc) -> tuple[str, str]:
    """An owner card running a pool worker's run, settled on both sides."""
    tid = kb.create_task(live, title="pool card", assignee="worker")
    _round(kanban_home, svc)
    run_uid = "01a1cedc-8b1c-7715-8c1f-57eefc9f55cd"
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": "w-run-1", "op": "upsert", "table": "task_runs", "uid": run_uid, "hlc": _hlc(1_000, OTHER),
         "fields": {"task_id": tid, "profile": "worker", "status": "running", "started_at": int(time.time())}},
        {"client_op_id": "w-claim-1", "op": "upsert", "table": "tasks", "uid": tid, "hlc": _hlc(1_001, OTHER),
         "fields": {"status": "running", "current_run_uid": run_uid}},
    ])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    return tid, run_uid


def _worker_ends(svc, tid, run_uid, *, status, outcome, completed_at=None):
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": f"w-end-run-{status}", "op": "upsert", "table": "task_runs", "uid": run_uid,
         "hlc": _hlc(2_000, OTHER),
         "fields": {"task_id": tid, "status": "done" if outcome == "completed" else outcome, "outcome": outcome,
                    "ended_at": int(time.time())}},
        {"client_op_id": f"w-end-task-{status}", "op": "upsert", "table": "tasks", "uid": tid,
         "hlc": _hlc(2_001, OTHER),
         "fields": {"status": status, "completed_at": completed_at, "current_run_uid": None,
                    "completion_contract": "https://github.com/o/r/pull/1"}},
    ])


def _live_missed_the_task_fields(live, kanban_home, svc, tid, run_uid):
    """The observed precondition: the replica and the live file agree on the card's Board version, the live file has
    the run's end, but its task row still holds the pre-completion fields (the regressing op's base_version was the
    replica's version and its fields were exactly the v16 values)."""
    before = dict(live.execute("SELECT status, completed_at, current_run_id, completion_contract FROM tasks "
                               "WHERE id = ?", (tid,)).fetchone())
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")  # no capture: the row only missed the feed
        live.execute("UPDATE tasks SET status = ?, completed_at = ?, current_run_id = ?, completion_contract = ? "
                     "WHERE id = ?", (before["status"], before["completed_at"], before["current_run_id"],
                                      before["completion_contract"], tid))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        replica = shadow.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    finally:
        shadow.close()
    assert live.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == replica
    assert live.execute("SELECT ended_at FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0] is not None


@pytest.mark.parametrize("status,outcome,completed_at", [
    ("done", "completed", 1794472039),  # t_01a1ceaeb6ba593c
    ("todo", "blocked", None),          # t_01a1ceb44e35f780
])
def test_divergence_repair_never_reopens_a_card_over_its_ended_run(live, kanban_home, svc, status, outcome,
                                                                  completed_at):
    tid, run_uid = _running_card(live, kanban_home, svc)
    _worker_ends(svc, tid, run_uid, status=status, outcome=outcome, completed_at=completed_at)
    _live_missed_the_task_fields(live, kanban_home, svc, tid, run_uid)

    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        repaired = ksc.repair_divergence(live, shadow, ME)
    finally:
        shadow.close()
    assert tid not in repaired  # the live copy is the stale one: nothing of it goes to the Board
    _round(kanban_home, svc)

    board = _service_task(svc, tid)
    assert board["status"] == status
    assert board["current_run_uid"] is None
    assert board["completion_contract"] == "https://github.com/o/r/pull/1"
    row = live.execute("SELECT status, current_run_id, completed_at, completion_contract FROM tasks WHERE id = ?",
                       (tid,)).fetchone()
    assert (row["status"], row["current_run_id"], row["completed_at"]) == (status, None, completed_at)
    assert row["completion_contract"] == "https://github.com/o/r/pull/1"


def test_divergence_repair_still_sends_a_real_uncaptured_edit_of_a_running_card(live, kanban_home, svc):
    """The guard is scoped to a stale lifecycle: a live edit with the run still open goes out as before."""
    tid, _run_uid = _running_card(live, kanban_home, svc)
    with kbc.write_txn(live):
        sync.drop_triggers(live)
        live.execute("UPDATE tasks SET title = 'renamed in a capture gap' WHERE id = ?", (tid,))
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        assert ksc.repair_divergence(live, shadow, ME) == {tid: ["title"]}
    finally:
        shadow.close()
