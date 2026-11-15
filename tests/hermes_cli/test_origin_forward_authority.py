"""Admission requires local accepted origin custody and a live server lease."""
import json
import sqlite3

import pytest

from hermes_cli.kanban_origin_ledger import _binding_key, _db, binding_hash


@pytest.fixture
def authority(tmp_path):
    origin = {"gateway": "macbook", "profile": "default", "session": "chat-1"}
    owner = {"gateway": "hub", "profile": "default", "board": "default", "task_id": "task-1"}
    binding = {"binding_id": "binding-1", "version": 1, "publication_version": 1,
               "owner": owner, "origin": origin, "task_record_key": "default/default/task-1"}
    path = tmp_path / "origin-ledger.db"
    with _db(path) as db:
        db.execute("INSERT INTO origin_obligations VALUES(?,?,?,?,?,?,?)",
                   (_binding_key(binding), binding_hash(binding), json.dumps(binding), "chat-1", "{}", 1, 0))
        db.commit()
    lease = {"task_id": "task-1", "holder": "worker-7", "run_uid": "run-1", "expires": 2000}
    return path, binding, lease


def check(authority, **overrides):
    from hermes_cli.kanban_origin_forward import authorize
    path, binding, lease = authority
    args = dict(ledger=path, origin=binding["origin"], creator=binding["owner"],
                gateway="macbook", profile="default", sender="gw:worker-7", run_uid="run-1",
                lease=lease, now_ms=1000, session_exists=lambda session: session == "chat-1")
    return authorize(**(args | overrides))


def test_foreign_worker_uses_origin_local_binding(authority):
    assert check(authority) == authority[1]


@pytest.mark.parametrize("field,value", [("holder", "worker-8"), ("task_id", "other"),
                                         ("run_uid", "old"), ("expires", 1000),
                                         ("expires", "2000"), ("expires", True)])
def test_wrong_or_expired_lease_rejected(authority, field, value):
    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        check(authority, lease=authority[2] | {field: value})


@pytest.mark.parametrize("override", [dict(gateway="worker-7"), dict(profile="other"),
                                      dict(sender="worker-7"), dict(sender="gw:"),
                                      dict(run_uid=""), dict(session_exists=lambda _: False)])
def test_wrong_local_identity_or_session_rejected(authority, override):
    with pytest.raises(ValueError, match="origin_forward_"):
        check(authority, **override)


@pytest.mark.parametrize("column,value", [("released", 1), ("binding_hash", "forged"),
                                         ("binding_id", "unqualified")])
def test_untrusted_or_released_binding_rejected(authority, column, value):
    with sqlite3.connect(authority[0]) as db:
        db.execute(f"UPDATE origin_obligations SET {column}=?", (value,))
    with pytest.raises(ValueError, match="origin_forward_binding_missing"):
        check(authority)


def test_unknown_owner_not_authorized_by_matching_chat(authority):
    with pytest.raises(ValueError, match="origin_forward_binding_missing"):
        check(authority, creator=authority[1]["owner"] | {"task_id": "forged"})


def test_missing_ledger_not_created(authority, tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        check(authority, ledger=path)
    assert not path.exists()


def test_server_lease_reader_pages_without_replica(authority):
    from hermes_cli.kanban_origin_forward import read_lease
    from types import SimpleNamespace
    calls = []

    def snapshot(board, table, after):
        calls.append((board, table, after))
        if not after:
            return {"table": "leases", "rows": [{"task_id": "earlier"}], "more": True, "next": "earlier"}
        return {"table": "leases", "rows": [authority[2]], "more": False, "next": "task-1"}

    hub = SimpleNamespace(snapshot_page=snapshot)
    assert read_lease(hub, "project", "task-1") == authority[2]
    assert calls == [("project", "leases", ""), ("project", "leases", "earlier")]
    assert read_lease(hub, "project", "missing") is None


@pytest.mark.parametrize("page", [None, {}, {"table": "tasks", "rows": [], "more": False},
    {"table": "leases", "rows": {}, "more": False},
    {"table": "leases", "rows": [], "more": 0},
    {"table": "leases", "rows": [], "more": True, "next": ""},
    {"table": "leases", "rows": [{"task_id": "a"}], "more": True, "next": "wrong"},
    {"table": "leases", "rows": [{"task_id": "a"}, {"task_id": "a"}], "more": False},
    {"table": "leases", "rows": [None], "more": False}])
def test_malformed_snapshot_not_treated_as_absence(page):
    from hermes_cli.kanban_origin_forward import read_lease
    from types import SimpleNamespace
    with pytest.raises(ValueError, match="origin_forward_lease_unavailable"):
        read_lease(SimpleNamespace(snapshot_page=lambda *a, **k: page), "default", "missing")


def test_unbounded_snapshot_does_not_authorize():
    from hermes_cli.kanban_origin_forward import read_lease
    from types import SimpleNamespace
    calls = []

    def snapshot(*args, after):
        calls.append(after)
        key = after + "a"
        return {"table": "leases", "rows": [{"task_id": key}], "more": True, "next": key}

    with pytest.raises(ValueError, match="origin_forward_lease_unavailable"):
        read_lease(SimpleNamespace(snapshot_page=snapshot), "default", "missing")
    assert len(calls) == 64
