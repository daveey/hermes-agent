"""Selective rejection retirement uses authoritative evidence, never replica absence."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME, OTHER = "default", "macbook", "mini"


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
    client.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def reject_status(live, svc):
    tid = kb.create_task(live, title="contested", assignee="worker")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    svc.board(BOARD)["leases"][tid] = {"task_id": tid, "holder": OTHER, "run_uid": "other-run", "expires": 9e15}
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    client.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert client.status(live)["rejected"] == 1
    return tid


def test_normal_sync_retires_superseded_keeps_audit_and_is_idempotent(live, svc, kanban_home):
    reject_status(live, svc)
    audits = [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE kind='sync_rejected'")]
    assert audits
    result = client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert result.rejection_resolutions[0]["classification"] == "superseded"
    assert result.rejection_resolutions[0]["action"] == "retired"
    assert client.status(live)["rejected"] == 0
    assert [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE kind='sync_rejected'")] == audits
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


def test_matching_lagging_replica_cannot_retire_unresolved(live, svc, kanban_home):
    tid = reject_status(live, svc)
    client._pull_replica(BOARD, svc.hub(ME), ME)
    svc.board(BOARD)["rows"]["tasks"][tid]["status"] = "done"
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    result = resolve_rejections(live, svc.hub(ME), BOARD)
    assert result[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert not client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


def ghost(live):
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error) "
                     "VALUES ('ghost-op','task_chat_links','ghost-link','upsert',?,'',1,'rejected','not_found')",
                     (json.dumps({"task_id": "t_ghost", "released": 1}),))


def test_unknown_task_link_retired_by_normal_sync(live, svc, kanban_home):
    ghost(live)
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert client.status(live)["rejected"] == 0


@pytest.mark.parametrize("offline", [False, True])
def test_cli_diff_does_not_report_clean_from_failed_refresh(live, svc, kanban_home, monkeypatch, capsys, offline):
    from argparse import Namespace
    from hermes_cli import kanban_sync_cli as cli
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(client, "Hub", lambda ident: svc.hub(ME))
    svc.offline = offline
    rc = cli._diff(Namespace(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["clean"] is (not offline)
    assert rc == int(offline)
    assert bool(report.get("unavailable")) is offline


def test_concurrent_edit_vetoes_retirement(live, svc, monkeypatch):
    reject_status(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read_then_change(*args):
        evidence = original(*args)
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_outbox SET attempts=attempts+1 WHERE state='rejected'")
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read_then_change)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)
    assert report[0]["classification"] == "unresolved"
    assert "changed" in report[0]["proof"]
    assert client.status(live)["rejected"] == 1


@pytest.mark.parametrize("veto", ["pending_create", "deleted", "history_compacted", "service_error"])
def test_moot_requires_proven_never_existed(live, svc, veto):
    ghost(live)
    if veto == "pending_create":
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('create-op','tasks','t_ghost','upsert','{}','',2)")
    elif veto == "deleted":
        svc.board(BOARD)["rows"].setdefault("tasks", {})["t_ghost"] = {"uid": "t_ghost", "deleted_at": 5}
    elif veto == "history_compacted":
        svc.board(BOARD)["floor"] = 1
    else:
        svc.offline = True
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    result = resolve_rejections(live, svc.hub(ME), BOARD)
    assert result[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1
