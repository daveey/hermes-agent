"""``kanban_sync_client.reconcile``: after a board file lost rows (a corruption rebuild), the board and the
Board service are brought back in step, including the unsent outbox the rebuild could not recover. [daveey]"""

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


def _without_capture(conn, sql, params=()):
    """A row vanishing from the file the way a rebuild loses it: no trigger, no outbox entry."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute(sql, params)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def test_reconcile_restores_lost_rows_and_resends_the_lost_outbox(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker")
    kb.add_comment(live, tid, "me", "synced note")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    # The rebuild lost a synced comment row and every pending outbox op (an unsent comment and a new card).
    _without_capture(live, "DELETE FROM task_comments WHERE body = 'synced note'")
    kb.add_comment(live, tid, "me", "unsent note")
    unsent = kb.create_task(live, title="unsent card", assignee="worker")
    live.execute("DELETE FROM kanban_sync_outbox")
    live.commit()

    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))

    assert report["restored"] == {"task_comments": 1}
    assert report["queued"]["tasks"] == 1 and report["queued"]["task_comments"] == 1
    assert report["rejected"] == 0 and report["clean"] is True
    bodies = {r[0] for r in live.execute("SELECT body FROM task_comments")}
    assert {"synced note", "unsent note"} <= bodies
    rows = svc.board(BOARD)["rows"]
    assert unsent in rows["tasks"]
    assert any(r["body"] == "unsent note" for r in rows["task_comments"].values())


def test_reconcile_never_reverts_a_local_edit_whose_outbox_entry_was_lost(live, svc, kanban_home):
    tid = kb.create_task(live, title="before", assignee="worker")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    live.execute("UPDATE tasks SET title = 'after' WHERE id = ?", (tid,))
    live.execute("DELETE FROM kanban_sync_outbox")
    live.commit()

    ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))

    assert live.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "after"


def test_reconcile_never_resends_a_row_the_service_deleted(live, svc, kanban_home):
    """The service's event GC tombstones old events of done cards; the file may still hold them."""
    tid = kb.create_task(live, title="card", assignee="worker")
    kb.add_comment(live, tid, "me", "gc'd on the service")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    uid = live.execute("SELECT uid FROM task_comments").fetchone()[0]
    svc.hub("mini").ops(BOARD, [{"client_op_id": "del-1", "op": "delete", "table": "task_comments", "uid": uid,
                                 "hlc": f"{9:013d}-0000-mini", "fields": {}}])

    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))

    assert "task_comments" not in report["queued"]
    assert svc.board(BOARD)["rows"]["task_comments"][uid].get("deleted_at")


def test_reconcile_never_restores_a_row_whose_local_delete_is_still_unsent(live, svc, kanban_home, monkeypatch):
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    kb.link_tasks(live, parent, child)
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert kb.unlink_tasks(live, parent, child)
    assert live.execute("SELECT count(*) FROM kanban_sync_tombstones WHERE table_name = 'task_links'").fetchone()[0]
    monkeypatch.setattr(ksc, "push", lambda *a, **k: ksc.PushResult())  # the service is unreachable for sends

    ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))

    assert live.execute("SELECT count(*) FROM task_links").fetchone()[0] == 0


def test_reconcile_is_a_no_op_on_a_board_in_step(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker")
    kb.add_comment(live, tid, "me", "note")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    calls = len(svc.calls)
    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    assert report["queued"] == {} and report["restored"] == {} and report["clean"] is True
    assert not [c for c in svc.calls[calls:] if c[0] == "ops"]


def test_cli_sync_reconcile_runs_against_the_service(live, svc, kanban_home, monkeypatch, capsys):
    import argparse
    import json

    from hermes_cli import kanban as kc
    tid = kb.create_task(live, title="card", assignee="worker")
    live.execute("DELETE FROM kanban_sync_outbox WHERE 1 = 1")
    live.commit()
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: svc.hub(ME).ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident, timeout=None: svc.hub(ident.gateway))
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="command"))
    rc = kc.kanban_command(parser.parse_args(["kanban", "sync", "reconcile", "--json"]))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["queued"] == {"tasks": 1, "task_events": 1} and out["clean"] is True
    assert tid in svc.board(BOARD)["rows"]["tasks"]


def test_reconcile_missing_stop_then_decide_ready_card(live, svc, kanban_home):
    from hermes_cli import kanban_spend_decision as decision
    tid = kb.create_task(live, title="Ready but held on Board", assignee="worker")
    with kb.write_txn(live):
        kb._append_event(live, tid, decision.EVENT, {"cause": "capability_unavailable"})
        kb._append_event(live, tid, "promoted", {})
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    stop = decision.pending_episode(live, tid)
    assert stop is not None
    uid = stop["uid"]
    _without_capture(live, "DELETE FROM task_events WHERE uid=?", (uid,))
    assert not decision.pending(live, tid)
    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    assert report["restored"] == {"task_events": 1}
    with ksc.open_shadow(BOARD, ME) as replica:
        assert decision.pending(replica, tid) and decision.pending(live, tid)
        assert decision.pending_episode(live, tid)["uid"] == uid
    assert kb.promote_task(live, tid, actor="operator", reason="Prerequisite delivered; bounded scope")[0]
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    with ksc.open_shadow(BOARD, ME) as replica:
        assert not decision.pending(replica, tid) and not decision.pending(live, tid)
