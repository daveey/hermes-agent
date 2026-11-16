"""Immutable forwarding authorization survives retries, never payload changes."""
import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.hermes_cli import test_origin_forward_authority as helpers

authority = helpers.authority


@pytest.fixture
def forwarding(authority, tmp_path):
    path, binding, lease = authority
    request = {"id": "placement:worker-7:request-1", "from": "gw:worker-7", "to_gw": "macbook",
               "coll": "sessions", "key": "default/chat-1", "action": "origin-forward",
               "args": {"origin": binding["origin"], "creator": binding["owner"], "run_uid": "run-1",
                        "destination": {"gateway": "hub", "profile": "default", "board": "default"},
                        "create": {"title": "child", "assignee": "default", "session_id": "owner-session"}}}
    calls = []

    def lookup(board, task):
        calls.append((board, task))
        return lease

    options = dict(path=tmp_path / "forward.db", ledger=path, gateway="macbook", profile="default",
                   lease_lookup=lookup, now_ms=lambda: 1000, session_exists=lambda sid: sid == "chat-1")
    return request, options, calls


def decide(forwarding, request=None, **overrides):
    from hermes_cli.kanban_origin_forward_decision import prepare
    original, options, _ = forwarding
    return prepare(original if request is None else request, **(options | overrides))


def test_forward_envelope_frozen_before_queue_and_survives_lease_end(forwarding):
    first = decide(forwarding)
    assert first["to_gw"] == "hub"
    assert first["coll"] == "tasks" and first["action"] == "create"
    assert first["key"] == "default/default"
    assert first["args"]["origin"]["gateway"] == "macbook"
    assert "from" not in first  # courier authenticates the actual MacBook, never copied worker identity
    assert first["args"]["create"] == forwarding[0]["args"]["create"]
    assert decide(forwarding, now_ms=lambda: 99999) == first
    assert forwarding[2] == [("default", "task-1")]
    with sqlite3.connect(forwarding[1]["path"]) as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["create", "destination", "run_uid", "creator"])
def test_same_id_changed_payload_conflicts(forwarding, field):
    decide(forwarding)
    changed = copy.deepcopy(forwarding[0])
    values = {"create": {"title": "changed"}, "destination": {"gateway": "other", "profile": "default", "board": "default"},
              "run_uid": "run-2", "creator": {"gateway": "hub", "profile": "default", "board": "default", "task_id": "other"}}
    changed["args"][field] = values[field]
    with pytest.raises(ValueError, match="origin_forward_conflict"):
        decide(forwarding, changed)


@pytest.mark.parametrize("field,value", [("to_gw", "worker-7"), ("key", "default/other"),
    ("action", "create"), ("coll", "tasks"), ("from", "worker-7"), ("id", "bad id")])
def test_wrong_route_never_authorizes(forwarding, field, value):
    with pytest.raises(ValueError, match="origin_forward_"):
        decide(forwarding, forwarding[0] | {field: value})
    assert not forwarding[2]


def test_unavailable_evidence_never_freezes_authorization(forwarding):
    def offline(*args):
        raise OSError("offline")
    with pytest.raises(OSError):
        decide(forwarding, lease_lookup=offline)
    with sqlite3.connect(forwarding[1]["path"]) as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert decide(forwarding)["args"]["origin"]["gateway"] == "macbook"


def test_sender_cannot_replay_other_workers_decision(forwarding):
    decide(forwarding)
    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        decide(forwarding, forwarding[0] | {"from": "gw:worker-8"})


def test_new_request_after_lease_expiry_refused(forwarding):
    decide(forwarding)
    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        decide(forwarding, forwarding[0] | {"id": "placement:worker-7:request-2"}, now_ms=lambda: 2000)


@pytest.mark.parametrize("destination", [{}, {"gateway": "hub", "profile": "default", "board": "../other"}, None])
def test_bad_destination_refused_without_authority_read(forwarding, destination):
    changed = copy.deepcopy(forwarding[0])
    changed["args"]["destination"] = destination
    with pytest.raises(ValueError, match="origin_forward_"):
        decide(forwarding, changed)
    assert not forwarding[2]


@pytest.mark.parametrize("expired_at", [2000, 3000])
def test_expiry_during_evidence_read_never_freezes_decision(forwarding, expired_at):
    clock = [1000]

    def session_exists(session):
        clock[0] = expired_at
        return session == "chat-1"

    with pytest.raises(ValueError, match="^origin_forward_lease_invalid$"):
        decide(forwarding, now_ms=lambda: clock[0], session_exists=session_exists)
    with sqlite3.connect(forwarding[1]["path"]) as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    # Refusal leaves no replay authorization; a later live attempt can succeed.
    assert decide(forwarding)["action"] == "create"


@pytest.mark.parametrize("expired_at", [2000, 3000])
def test_expiry_while_waiting_for_write_lock_never_freezes_decision(forwarding, monkeypatch, expired_at):
    path = forwarding[1]["path"]
    connect = sqlite3.connect
    clock = [1000]
    attempting_write = threading.Event()

    def traced_connect(database, *args, **kwargs):
        db = connect(database, *args, **kwargs)
        if database == path:
            db.set_trace_callback(lambda sql: attempting_write.set() if sql == "BEGIN IMMEDIATE" else None)
        return db

    # Seed the schema so startup DDL does not take the contested write lock.
    with connect(path) as blocker:
        blocker.execute("CREATE TABLE decisions (identity TEXT PRIMARY KEY, "
                        "request_json TEXT NOT NULL, envelope_json TEXT NOT NULL)")
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(sqlite3, "connect", traced_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(decide, forwarding, now_ms=lambda: clock[0])
            try:
                assert attempting_write.wait(5), "admission never reached the contested lock"
                assert not future.done(), "admission bypassed the held write lock"
                clock[0] = expired_at
            finally:
                blocker.rollback()
            with pytest.raises(ValueError, match="^origin_forward_lease_invalid$"):
                future.result(timeout=5)
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
