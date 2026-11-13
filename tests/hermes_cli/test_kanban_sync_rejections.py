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


@pytest.mark.parametrize("veto", ["version", "pending", "identity", "untranslated_run", "local_change"])
def test_superseded_requires_settled_identity_and_local_snapshot(live, svc, monkeypatch, veto):
    tid = reject_status(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read(*args):
        evidence = original(*args)
        if veto == "version":
            evidence["row"]["version"] += 1
        elif veto == "identity":
            evidence["uid"] = "another-card"
        elif veto == "local_change":
            with kbc.write_txn(live):
                live.execute("UPDATE tasks SET title='changed' WHERE id=?", (tid,))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    with kbc.write_txn(live):
        if veto == "pending":
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('pending','tasks',?,'upsert','{}','',2)", (tid,))
        elif veto == "untranslated_run":
            live.execute("UPDATE kanban_sync_state SET applying=1")
            live.execute("UPDATE tasks SET current_run_id=999999 WHERE id=?", (tid,))
            live.execute("UPDATE kanban_sync_state SET applying=0")
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)
    assert report[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1


def test_cli_diff_dirty_then_clean_after_normal_sync(live, svc, kanban_home, monkeypatch, capsys):
    from argparse import Namespace
    from hermes_cli import kanban_sync_cli as cli
    reject_status(live, svc)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(client, "Hub", lambda ident: svc.hub(ME))
    assert cli._diff(Namespace(json=True)) == 1
    assert json.loads(capsys.readouterr().out)["clean"] is False
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert cli._diff(Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


@pytest.mark.parametrize("table,translated,settled", [
    ("task_events", False, True), ("task_events", True, True),
    ("task_runs", True, False), ("task_runs", True, True),
])
def test_child_reference_translation_and_run_version(live, svc, table, translated, settled):
    tid = kb.create_task(live, title="child controls", assignee="worker")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("INSERT INTO task_runs (id,uid,task_id,status,sync_version,started_at) VALUES (123,'run-uid',?,'done',?,1)",
                     (tid, 3 if settled else 2))
        if table == "task_events":
            live.execute("INSERT INTO task_events (uid,task_id,run_id,kind,created_at) VALUES ('event-uid',?,?,'done',1)",
                         (tid, 123 if translated else 999))
        live.execute("UPDATE kanban_sync_state SET applying=0")
        uid = "event-uid" if table == "task_events" else "run-uid"
        fields = {"run_uid": "old-run"} if table == "task_events" else {"status": "blocked"}
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                     "VALUES ('child-op',?,?,'upsert',?,'',1,'rejected')", (table, uid, json.dumps(fields)))
    remote = {"uid": uid, "version": 3, "status": "done", "run_uid": "run-uid" if translated else None}
    svc.board(BOARD)["rows"].setdefault(table, {})[uid] = remote
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    reports = resolve_rejections(live, svc.hub(ME), BOARD)
    expected = "superseded" if translated and settled else "unresolved"
    assert reports[0]["classification"] == expected
    assert client.status(live)["rejected"] == int(expected == "unresolved")


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


TASK, RUN = "t_released", "run-reclaimed"


def lost_release(live, svc, *, error="lease_lost: this run no longer holds the lease"):
    """worker-1 11-13 shape: the host's core-update reclaim ended the run on the Board ~3 s before the dead
    worker's own crash release was pushed, so that release was refused ``lease_lost`` and kept forever."""
    fields = {"failure_limit": 2, "force_trip": False, "infrastructure": False, "block_kind": "transient",
              "diagnostic": {"exit_kind": "signaled", "exit_code": 15}, "run_uid": RUN, "outcome": "crashed",
              "error": "pid 3264955 killed by signal 15"}
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error,"
                     "attempts) VALUES ('release-op','tasks',?,'release',?,'',1,'rejected',?,1)",
                     (TASK, json.dumps(fields), error))
    rows = svc.board(BOARD)["rows"]
    rows.setdefault("tasks", {})[TASK] = {"uid": TASK, "id": TASK, "status": "done", "version": 66, "created_on": OTHER,
                                          "current_run_uid": None, "lease_run_uid": None, "lease_holder": None}
    rows.setdefault("task_runs", {})[RUN] = {"uid": RUN, "task_id": TASK, "status": "reclaimed", "outcome": "reclaimed",
                                             "ended_at": 1794527437, "version": 4, "origin_gateway": ME}
    return rows


def test_lease_lost_release_of_ended_run_retired_by_normal_sync(live, svc, kanban_home):
    lost_release(live, svc)
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["rejected_ops"] == 1
    finally:
        shadow.close()
    result = client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    report = result.rejection_resolutions[0]
    assert (report["classification"], report["action"]) == ("moot", "retired")
    assert report["run_evidence"]["row"]["ended_at"] == 1794527437
    assert client.status(live)["rejected"] == 0
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


RELEASE_BOARD_VETOES = {
    "run_open": lambda rows: rows["task_runs"][RUN].update(ended_at=None),
    "lease_held": lambda rows: rows["tasks"][TASK].update(lease_run_uid=RUN),
    "current_run": lambda rows: rows["tasks"][TASK].update(current_run_uid=RUN),
    "other_task": lambda rows: rows["task_runs"][RUN].update(task_id="t_other"),
    "run_missing": lambda rows: rows["task_runs"].pop(RUN),
    "run_deleted": lambda rows: rows["task_runs"][RUN].update(deleted_at=1),
    "task_missing": lambda rows: rows["tasks"].pop(TASK),
}


@pytest.mark.parametrize("veto", [*RELEASE_BOARD_VETOES, "not_lease_lost", "local_run_open", "pending_run",
                                  "evidence_down"])
def test_release_retirement_needs_ended_run_off_the_lease(live, svc, monkeypatch, veto):
    rows = lost_release(live, svc, error="not_found: no such card" if veto == "not_lease_lost" else
                        "lease_lost: this run no longer holds the lease")
    RELEASE_BOARD_VETOES.get(veto, lambda rows: None)(rows)
    hub = svc.hub(ME)
    tid = None
    if veto in ("local_run_open", "pending_run"):
        tid = kb.create_task(live, title="local copy", assignee="worker")
    with kbc.write_txn(live):
        if veto in ("local_run_open", "pending_run"):
            live.execute("UPDATE kanban_sync_state SET applying=1")
            live.execute("INSERT INTO task_runs (id,uid,task_id,status,started_at) VALUES (77,?,?,'running',1)", (RUN, tid))
            live.execute("UPDATE kanban_sync_state SET applying=0")
        if veto == "pending_run":
            live.execute("UPDATE task_runs SET ended_at=2 WHERE uid=?", (RUN,))
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('run-pending','task_runs',?,'upsert','{}','',2)", (RUN,))
    if veto == "evidence_down":
        original = hub.row_evidence

        def read(board, table, uid):
            if table == "task_runs":
                raise OSError("board unreachable")
            return original(board, table, uid)
        monkeypatch.setattr(hub, "row_evidence", read)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("unresolved", "retained")
    assert live.execute("SELECT state FROM kanban_sync_outbox WHERE op_id='release-op'").fetchone()[0] == "rejected"


def test_live_lease_by_the_run_keeps_release_end_to_end(live, kanban_home):
    """The ``lease_held`` veto through real Board transitions, not a hand-set row: the grant mirrors the lease
    onto the card (setLease), the holder then ends its run row and drops ``current_run_uid`` while keeping the
    lease, so only ``lease_run_uid`` still names the run. Once the holder leaves ``running`` (clearLease), the same
    release is moot. End-to-end on the Board side only: the rejected local release op is still inserted by hand."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    granted = hub.leases(BOARD, {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "grant-op"})
    assert granted["status"] == "applied"
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["lease_run_uid"], card["lease_holder"]) == (RUN, ME)

    def holder(op_id, table, uid, fields):
        res = hub.ops(BOARD, [{"client_op_id": op_id, "table": table, "uid": uid, "op": "upsert", "fields": fields,
                               "hlc": f"{9_999_999_999_999:013d}-0001-{ME}"}])["results"][0]
        assert res["status"] == "applied", res

    holder("run-end", "task_runs", RUN, {"ended_at": 1794527437, "status": "crashed", "outcome": "crashed"})
    holder("drop-current", "tasks", tid, {"current_run_uid": None})
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["status"], card["current_run_uid"], card["lease_run_uid"]) == ("running", None, RUN)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error,"
                     "attempts) VALUES ('release-op','tasks',?,'release',?,'',1,'rejected',?,1)",
                     (tid, json.dumps({"run_uid": RUN, "outcome": "crashed"}),
                      "lease_lost: this run no longer holds the lease"))
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("unresolved", "retained")
    assert report["evidence"]["row"]["lease_run_uid"] == RUN
    assert client.status(live)["rejected"] == 1

    holder("leave-running", "tasks", tid, {"status": "blocked"})
    assert svc.board(BOARD)["rows"]["tasks"][tid]["lease_run_uid"] is None
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("moot", "retired")
    assert client.status(live)["rejected"] == 0


def test_fake_lease_release_after_run_end_clears_mirror(live, kanban_home):
    """As releaseTask's clearLease: releasing a lease whose run row already ended still drops lease_run_uid."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    assert hub.leases(BOARD, {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g"})["status"] == "applied"
    res = hub.ops(BOARD, [{"client_op_id": "end", "table": "task_runs", "uid": RUN, "op": "upsert",
                           "fields": {"ended_at": 1794527437, "status": "crashed", "outcome": "crashed"},
                           "hlc": f"{9_999_999_999_999:013d}-0001-{ME}"}])["results"][0]
    assert res["status"] == "applied", res
    assert hub.leases(BOARD, {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r"})["status"] == "applied"
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["lease_holder"], card["lease_run_uid"], card["lease_expires"]) == (None, None, None)


def test_release_local_change_during_read_vetoes(live, svc, monkeypatch):
    lost_release(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read_then_open_run(board, table, uid):
        evidence = original(board, table, uid)
        if table == "task_runs":
            with kbc.write_txn(live):
                live.execute("INSERT INTO kanban_sync_tombstones (table_name,row_uid,deleted_at) VALUES ('task_runs',?,1)",
                             (RUN,))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read_then_open_run)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)[0]
    assert report["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1


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
