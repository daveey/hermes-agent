"""Explicit abandonment of containment-refused deletes retains canonical dependencies."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME = "default", "macbook"
ERROR = "containment_hold: use scoped resume-contained; generic writes cannot satisfy containment"
OP_IDS = ("01a1f721-13c4-713c-ad27-0db16bc7e944", "01a1f724-8fe9-73b5-9782-adec705a8120",
          "01a1f741-6dcf-7c33-9b92-d6fce5f88ca6")


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    live = kbc.connect(home / "kanban.db")
    client.ensure_identity(live, BOARD, ME)
    sync.set_mode(live, "primary")
    svc = FakeService()
    hub = svc.hub(ME)
    parent = kb.create_task(live, title="released parent", assignee="worker")
    child = kb.create_task(live, title="leased child", assignee="worker")
    kb.link_tasks(live, parent, child)
    client.sync_board(BOARD, home / "kanban.db", hub, ME)
    uid = parent + ">" + child
    canonical_child = svc._put(svc.board(BOARD), "tasks", child,
                               {"status": "running", "lease_holder": "worker-1", "lease_expires": 9999999999999},
                               "worker-1", None, "9999999999998-0000-worker-1")
    svc.board(BOARD)["leases"][child] = {"task_id": child, "holder": "worker-1", "run_uid": "active-worker", "expires": 9999999999999}
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("UPDATE tasks SET status='running',lease_holder='worker-1',lease_expires=9999999999999,sync_version=? WHERE id=?",
                     (canonical_child["version"], child))
        live.execute("UPDATE kanban_sync_state SET applying=0")
        for i, op in enumerate(OP_IDS):
            live.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,state,"
                         "last_error,attempts) VALUES (?,'task_links',?,'delete',?,'',?,'rejected',?,1)",
                         (op, uid, '{ }' if i == 0 else '{}', 1000 + i * 1000, ERROR))
            live.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,'unlinked',?,?)",
                         (f"historical-{i}", child, json.dumps({"parent": parent, "child": child}), i + 1))
        live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_links',?,3000)", (uid,))
        # Historical events already synced; their original payloads are retained.
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name='task_events'")
    original_evidence = hub.row_evidence
    monkeypatch.setattr(hub, "row_evidence", lambda b, t, u: {"board": b, **original_evidence(b, t, u)})
    def context(board, task, producer=None):
        return {"version": 1, "board": board, "target": {"board": board, "gateway": ME, "task_id": task}, "held": False}
    monkeypatch.setattr(hub, "containment_context", context, raising=False)
    yield live, hub, svc, uid, parent, child
    live.close()


def rows(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def test_three_deletes_preview_apply_and_normal_resolver_preserve_bytes(case):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, hub, _svc, uid, _parent, _child = case
    before = {t: rows(live, t) for t in ("tasks", "task_links", "task_events", "kanban_sync_outbox")}
    preview = recovery.plan(live, hub, BOARD, uid, "abandon refused historical unlink")
    assert preview["eligible"], preview
    assert preview["action"] == "preview"
    assert rows(live, "kanban_sync_outbox") == before["kanban_sync_outbox"]
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_sync_dependency_recoveries'").fetchone()
    applied = recovery.apply(live, hub, BOARD, uid, preview["token"], "abandon refused historical unlink")
    assert applied["action"] == "applied", applied
    for table, original_rows in before.items():
        assert rows(live, table) == original_rows
    assert not rows(live, "kanban_sync_tombstones")
    receipt = rows(live, "kanban_sync_dependency_recoveries")[0]
    snapshot = json.loads(receipt["snapshot_json"])
    assert snapshot["ops"] == before["kanban_sync_outbox"]
    assert snapshot["events"] == [e for e in before["task_events"] if e["kind"] == "unlinked"]
    reports = resolve_rejections(live, hub, BOARD)
    assert len(reports) == 3
    assert all(r["action"] == "retired" for r in reports), reports
    assert not rows(live, "kanban_sync_outbox")
    assert rows(live, "task_links") == before["task_links"]
    assert rows(live, "tasks") == before["tasks"]
    assert rows(live, "task_events") == before["task_events"]
    assert rows(live, "kanban_sync_dependency_recoveries") == [receipt]
    assert resolve_rejections(live, hub, BOARD) == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        live.execute("UPDATE kanban_sync_dependency_recoveries SET reason='changed'")


def test_normal_round_settles_receipt_while_holding_sync_lock(case, monkeypatch):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _svc, uid, parent, child = case
    preview = recovery.plan(live, hub, BOARD, uid, "retain canonical link")
    assert preview["eligible"], preview
    assert recovery.apply(live, hub, BOARD, uid, preview["token"], "retain canonical link")["action"] == "applied"
    path = live.execute("PRAGMA database_list").fetchone()[2]
    result = client.sync_board(BOARD, Path(path), hub, ME)
    assert not rows(live, "kanban_sync_outbox"), result.rejection_resolutions
    assert len(rows(live, "kanban_sync_dependency_recoveries")) == 1
    assert rows(live, "task_links") == [{"parent_id": parent, "child_id": child}]


def test_new_progress_event_does_not_strand_receipted_deletes(case):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, hub, _, uid, _, child = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")["action"] == "applied"
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES ('later',?,'progress','{}',99)", (child,))
        live.execute("UPDATE kanban_sync_state SET applying=0")
    assert all(r["action"] == "retired" for r in resolve_rejections(live, hub, BOARD))
    assert live.execute("SELECT 1 FROM task_events WHERE uid='later'").fetchone()


def test_missing_unlink_audit_refuses_receipt(case):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _, uid, _, _ = case
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("DELETE FROM task_events WHERE kind='unlinked'")
        live.execute("UPDATE kanban_sync_state SET applying=0")
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert p.get("reason") == "missing_unlink_audit", p


def test_corrupt_receipt_retains_ops_instead_of_crashing_sync(case):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, hub, _, uid, _, _ = case
    with kbc.write_txn(live):
        recovery._schema(live)
        live.execute("INSERT INTO kanban_sync_dependency_recoveries VALUES ('broken',?,?,?,'reason','{','bad','{}')",
                     (BOARD, uid, recovery.keys._endpoint(hub)))
    reports = resolve_rejections(live, hub, BOARD)
    assert len(reports) == 3
    assert all(r["action"] == "retained" for r in reports)


@pytest.mark.parametrize("column,value", [("last_error", "containment_hold: different reason"),
    ("state", "pending"), ("op", "upsert"), ("fields", '{"parent_id":"wrong"}')])
def test_only_exact_refused_empty_deletes_eligible(case, column, value):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _, uid, _, _ = case
    with kbc.write_txn(live):
        live.execute(f"UPDATE kanban_sync_outbox SET {column}=? WHERE op_id=?", (value, OP_IDS[0]))
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert p.get("reason") == "unsupported_link_work", p
    assert len(rows(live, "kanban_sync_outbox")) == 3


@pytest.mark.parametrize("defect,reason", [("pending_endpoint", "endpoint_local_work"),
    ("tombstone", "uncovered_tombstone"), ("missing_audit", "missing_unlink_audit"),
    ("wrong_audit", "missing_unlink_audit"), ("audit_clock", "missing_unlink_audit"),
    ("audit_uid", "missing_unlink_audit"), ("local_hold", "local_containment_hold")])
def test_local_vetoes_preserve_every_input(case, defect, reason):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _, uid, parent, _child = case
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        statements = {
            "pending_endpoint": ("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                                 "VALUES ('newer','tasks',?,'upsert','{}','',9999)", (parent,)),
            "tombstone": ("UPDATE kanban_sync_tombstones SET deleted_at=9999", ()),
            "missing_audit": ("DELETE FROM task_events WHERE uid='historical-1'", ()),
            "wrong_audit": ("UPDATE task_events SET payload='{}' WHERE uid='historical-1'", ()),
            "audit_clock": ("UPDATE task_events SET created_at=99 WHERE uid='historical-1'", ()),
            "audit_uid": ("UPDATE task_events SET uid=NULL WHERE uid='historical-1'", ()),
            "local_hold": ("UPDATE tasks SET containment_episode='current-episode' WHERE id=?", (parent,)),
        }
        sql, params = statements[defect]
        live.execute(sql, params)
        live.execute("UPDATE kanban_sync_state SET applying=0")
    before = {t: rows(live, t) for t in ("tasks", "task_events", "task_links", "kanban_sync_outbox", "kanban_sync_tombstones")}
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert p.get("reason") == reason, p
    assert all(rows(live, t) == v for t, v in before.items())


@pytest.mark.parametrize("defect,reason", [("uid", "authoritative_identity_changed"),
    ("row_uid", "authoritative_version_changed"), ("version", "authoritative_version_changed"),
    ("parent", "canonical_link_changed"), ("deleted", "authoritative_version_changed"),
    ("endpoint_version", "endpoint_identity_changed"), ("endpoint_origin", "endpoint_identity_changed"),
    ("canonical_hold", "canonical_containment_hold"), ("feed", "authoritative_feed_changed"),
    ("busy", "diagnostic_refresh_incomplete"), ("behind", "diagnostic_refresh_incomplete")])
def test_authoritative_vetoes(case, monkeypatch, defect, reason):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _svc, uid, parent, child = case
    read = hub.row_evidence
    def evidence(board, table, key):
        e = read(board, table, key)
        if table == "task_links":
            if defect == "uid": e["uid"] = "wrong"
            if defect == "row_uid": e["row"]["uid"] = "wrong"
            if defect == "version": e["row"]["version"] = True
            if defect == "parent": e["row"]["parent_id"] = child
            if defect == "deleted": e["row"]["deleted_at"] = 99
        elif key == parent:
            if defect == "endpoint_version": e["row"]["version"] += 1
            if defect == "endpoint_origin": e["row"]["created_on"] = "other-gateway"
        return e
    monkeypatch.setattr(hub, "row_evidence", evidence)
    if defect == "canonical_hold":
        old = hub.containment_context
        monkeypatch.setattr(hub, "containment_context", lambda *a: {**old(*a), "held": True})
    elif defect == "feed":
        old = hub.changes
        def changed(board, cursor, limit=500):
            result = old(board, cursor, limit)
            if result["changes"] == []:
                result["head"] += 1
            return result
        monkeypatch.setattr(hub, "changes", changed)
    elif defect in ("busy", "behind"):
        monkeypatch.setattr(client, "_pull_replica", lambda *a, **kw: client.Pulled(busy=defect == "busy", more=defect == "behind"))
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert p.get("reason") == reason, p
    assert len(rows(live, "kanban_sync_outbox")) == 3


@pytest.mark.parametrize("change", ["reason", "token", "version", "local", "during_read"])
def test_apply_revalidates_preview_and_evidence(case, monkeypatch, change):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, svc, uid, _parent, _child = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert p["eligible"], p
    reason = "new reason" if change == "reason" else "retain link"
    token = "not-the-preview" if change == "token" else p["token"]
    if change == "version":
        svc.board(BOARD)["rows"]["task_links"][uid]["version"] += 1
    if change == "local":
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_outbox SET attempts=2 WHERE op_id=?", (OP_IDS[0],))
    if change == "during_read":
        original = hub.row_evidence
        def read(*args):
            e = original(*args)
            with kbc.write_txn(live):
                live.execute("UPDATE kanban_sync_outbox SET attempts=attempts+1 WHERE op_id=?", (OP_IDS[0],))
            return e
        monkeypatch.setattr(hub, "row_evidence", read)
    r = recovery.apply(live, hub, BOARD, uid, token, reason)
    assert r["action"] == "refused", r
    assert r["reason"] == ("local_inputs_changed" if change == "during_read" else "preview_token_changed")
    assert len(rows(live, "kanban_sync_outbox")) == 3
    assert rows(live, "kanban_sync_tombstones")


def test_link_version_change_after_apply_prevents_retirement(case):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, hub, svc, uid, _, _ = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")["action"] == "applied"
    svc.board(BOARD)["rows"]["task_links"][uid]["version"] += 1
    reports = resolve_rejections(live, hub, BOARD)
    assert all(r["action"] == "retained" and r["proof"] == "receipt_link_changed" for r in reports)


def test_cli_preview_apply(case, monkeypatch, capsys):
    from argparse import Namespace
    from hermes_cli import kanban_sync_cli as cli
    live, hub, _, uid, parent, child = case
    path = Path(live.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(cli, "_board", lambda _: BOARD)
    monkeypatch.setattr(cli, "_path", lambda _: path)
    monkeypatch.setattr(client, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(client, "Hub", lambda _: hub)
    args = Namespace(sync_action="dependency-delete", row_uid=uid, reason="keep link", apply=False, expect=None, json=True)
    assert cli.cmd_sync(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["eligible"]
    args.apply = True
    args.expect = preview["token"]
    assert cli.cmd_sync(args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["action"] == "applied"
    assert rows(live, "task_links") == [{"parent_id": parent, "child_id": child}]


def test_parser_exposes_explicit_per_link_preview():
    import argparse
    from hermes_cli.kanban_parser import build_parser
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "sync", "dependency-delete", "t_parent>t_child", "--reason", "retain link", "--json"])
    assert args.sync_action == "dependency-delete"
    assert args.row_uid == "t_parent>t_child"
    assert not args.apply and args.expect is None


@pytest.mark.parametrize("at_commit", [False, True])
def test_evidence_age_includes_writer_lock_wait(case, monkeypatch, at_commit):
    import contextlib
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _, uid, _, _ = case
    clock = [0.0]
    monkeypatch.setattr(recovery.keys, "_mono", lambda: clock[0])
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    if at_commit:
        transaction = recovery.write_txn
        @contextlib.contextmanager
        def delayed(conn):
            with transaction(conn):
                clock[0] += 31
                yield
        monkeypatch.setattr(recovery, "write_txn", delayed)
    else:
        read = hub.row_evidence
        def slow(*args):
            e = read(*args)
            clock[0] += 11
            return e
        monkeypatch.setattr(hub, "row_evidence", slow)
    r = recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")
    assert r.get("reason") == "evidence_expired", r
    assert len(rows(live, "kanban_sync_outbox")) == 3
    assert rows(live, "kanban_sync_tombstones")
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_sync_dependency_recoveries'").fetchone()


def test_local_cas_after_transaction_entry(case, monkeypatch):
    import contextlib
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, _, uid, _parent, _ = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    transaction = recovery.write_txn
    @contextlib.contextmanager
    def interleaved(conn):
        with transaction(conn):
            conn.execute("UPDATE kanban_sync_outbox SET attempts=2 WHERE op_id=?", (OP_IDS[0],))
        with transaction(conn):
            yield
    monkeypatch.setattr(recovery, "write_txn", interleaved)
    r = recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")
    assert r.get("reason") == "local_inputs_changed", r
    assert rows(live, "kanban_sync_tombstones")


@pytest.mark.parametrize("defect", ["new_delete", "changed_bytes", "changed_audit", "held", "new_tombstone"])
def test_settlement_does_not_hide_new_work(case, defect):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, hub, _, uid, parent, _child = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    assert recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")["action"] == "applied"
    statements = {
        "new_delete": ("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error) "
                       "VALUES ('new','task_links',?,'delete','{}','',9999,'rejected',?)", (uid, ERROR)),
        "changed_bytes": ("UPDATE kanban_sync_outbox SET attempts=999 WHERE op_id=?", (OP_IDS[0],)),
        "changed_audit": ("UPDATE task_events SET created_at=99 WHERE uid='historical-1'", ()),
        "held": ("UPDATE tasks SET containment_episode='unreleased' WHERE id=?", (parent,)),
        "new_tombstone": ("INSERT INTO kanban_sync_tombstones VALUES ('task_links',?,9999)", (uid,)),
    }
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute(*statements[defect])
        live.execute("UPDATE kanban_sync_state SET applying=0")
    before = rows(live, "kanban_sync_outbox")
    reports = resolve_rejections(live, hub, BOARD)
    assert all(r["action"] == "retained" for r in reports), reports
    assert rows(live, "kanban_sync_outbox") == before


def test_canonical_change_during_evidence_reads_vetoes_apply(case, monkeypatch):
    from hermes_cli import kanban_sync_dependency_delete as recovery
    live, hub, svc, uid, parent, child = case
    p = recovery.plan(live, hub, BOARD, uid, "retain link")
    read = hub.row_evidence
    def racing(*args):
        e = read(*args)
        if args[1] == "tasks" and args[2] == child:
            svc._put(svc.board(BOARD), "task_links", uid, {"parent_id": parent, "child_id": child}, ME, None,
                     "9999999999998-0000-macbook")
        return e
    monkeypatch.setattr(hub, "row_evidence", racing)
    r = recovery.apply(live, hub, BOARD, uid, p["token"], "retain link")
    assert r.get("reason") == "authoritative_feed_changed", r
    assert rows(live, "kanban_sync_tombstones")
