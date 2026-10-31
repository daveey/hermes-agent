"""A capture gap on an attached board must not fork a claim's run.

Live failure (hermes-hub, 2026-10-30 23:43): a probe script run by a Kanban worker set ``HERMES_HOME`` to a temp dir
but inherited the worker's ``HERMES_KANBAN_DB`` (the live board). Its kanban home had no ``kanban.sync`` setting, so
opening the board dropped every capture trigger. A dispatcher claim inside that ~50 s gap left the lease's run uid
in ``kanban_sync_state.pending_run_uid`` (no insert trigger consumed it), the run row got a different uid, the
Board's echo of the claim then made a second ``pending_sync`` run row with the lease uid (one dispatch, two runs; the
crash ended the wrong one and the first stayed ``running`` forever), and the next local run insert on ANY card
(a manual ``kanban complete``) took the stale pending uid and failed ``UNIQUE constraint failed: task_runs.uid``.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "hub", "default"


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


def _triggers(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                       "AND name LIKE 'kanban_sync_%'")}


def _round(kanban_home, svc):
    res = ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert res.error is None and (res.push is None or res.push.rejected == 0)
    return res


def _reopen_from_foreign_home(tmp_path, monkeypatch, db, *, config: str | None) -> None:
    """A fresh process whose HERMES_HOME is a temp dir but whose HERMES_KANBAN_DB pins the live board."""
    other = tmp_path / "probe-home"
    other.mkdir(exist_ok=True)
    if config is not None:
        (other / "config.yaml").write_text(config)
    monkeypatch.setenv("HERMES_HOME", str(other))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    sync._SYNCED_PATHS.clear()
    with kbc.connect_closing(db):
        pass


@pytest.mark.parametrize("config", [None, "model: x\n", "kanban:\n  dispatch_interval_seconds: 5\n"])
def test_a_process_whose_home_says_nothing_about_sync_keeps_an_attached_boards_triggers(
        live, kanban_home, tmp_path, monkeypatch, config):
    db = kanban_home / "kanban.db"
    before = _triggers(live)
    assert before
    _reopen_from_foreign_home(tmp_path, monkeypatch, db, config=config)
    assert _triggers(live) == before


@pytest.mark.parametrize("config", ["kanban:\n  sync:\n    enabled: false\n",
                                    "kanban:\n  sync:\n    enabled: true\n    boards: [other]\n"])
def test_an_explicit_off_still_detaches_an_attached_board(live, kanban_home, tmp_path, monkeypatch, config):
    _reopen_from_foreign_home(tmp_path, monkeypatch, kanban_home / "kanban.db", config=config)
    assert _triggers(live) == set()


def test_a_claim_without_capture_triggers_keeps_one_run_and_leaves_no_pending_uid(live, kanban_home, svc):
    tid = kb.create_task(live, title="claimed in a capture gap", assignee="worker")
    other = kb.create_task(live, title="completed by hand afterwards", assignee="worker")
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # the gap: some process detached the board
    claimed = kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    assert claimed is not None
    lease_uid = svc.board(BOARD)["leases"][tid]["run_uid"]
    run = live.execute("SELECT id, uid FROM task_runs WHERE id = ?", (claimed.current_run_id,)).fetchone()
    assert run["uid"] == lease_uid
    assert live.execute("SELECT pending_run_uid FROM kanban_sync_state").fetchone()[0] is None
    # The gap closes (the next normal process reinstalls the triggers) and the Board's echo comes back.
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing(kanban_home / "kanban.db"):
        pass
    assert _triggers(live)
    _round(kanban_home, svc)
    runs = live.execute("SELECT id, uid, status FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
    assert [(r["id"], r["uid"]) for r in runs] == [(run["id"], lease_uid)], [dict(r) for r in runs]
    assert kb.get_task(live, tid).current_run_id == run["id"]
    # The next local run insert, on another card, is not handed the claim's uid.
    assert kb.complete_task(live, other, summary="done by hand", force=True)
    assert len({r[0] for r in live.execute("SELECT uid FROM task_runs")}) == \
        live.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]


def test_a_board_echo_pulled_before_the_claim_txn_is_adopted_as_the_run(live, kanban_home, svc, monkeypatch):
    """The lease grant's echo can land between ``claim_gate`` and the claim transaction; its placeholder row is
    the run, not a second one."""
    tid = kb.create_task(live, title="echo first", assignee="worker")
    _round(kanban_home, svc)
    real_gate = sync.claim_gate

    def gate_then_pull(conn, task_id, expires, lane="ready"):
        gate = real_gate(conn, task_id, expires, lane)
        with kbc.write_txn(conn):
            conn.execute("UPDATE kanban_sync_state SET applying = 1")
            sync._local_run_id(conn, gate.lease.run_uid, task_id)
            conn.execute("UPDATE kanban_sync_state SET applying = 0")
        return gate

    monkeypatch.setattr(sync, "claim_gate", gate_then_pull)
    claimed = kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    assert claimed is not None
    runs = live.execute("SELECT id, uid, status FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
    assert len(runs) == 1 and runs[0]["status"] == "running" and runs[0]["id"] == claimed.current_run_id
