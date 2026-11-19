"""Adjacent negative controls for internal authenticated replica scopes."""
from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace

import pytest

from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def cached(conn):
    target, child, stop, release, hub = fixtures.scenario(conn)
    change = fixtures.event(stop, hub, 1)
    replica.certify_page(conn, hub, "fixture", [change])
    return target, child, stop, release, hub, change


def permitted(conn, change):
    return replica.event_permit(conn, change.uid, change.fields["task_id"],
                                change.fields["kind"], change.fields["payload"])


@pytest.mark.parametrize("kind", ["contained", "containment_released"])
def test_certification_rejects_history_delete(synced_conn, kind):
    conn = synced_conn
    _, _, stop, release, hub = fixtures.scenario(conn)
    op = stop if kind == "contained" else release
    change = fixtures.event(op, hub, 1)
    replica.certify_page(conn, hub, "fixture", [change])
    with pytest.raises(ContainmentRefused, match="cannot be deleted"):
        replica.certify_page(conn, hub, "fixture", [replace(change, op="delete")])


@pytest.mark.parametrize("fault", ["table", "uid", "op", "origin", "payload", "board"])
def test_event_permit_requires_each_wire_coordinate(synced_conn, fault):
    conn = synced_conn
    *_, change = cached(conn)
    with kb.write_txn(conn):
        with replica.projection(conn, change):
            assert permitted(conn, change) == 1
            assert replica.event_permit(conn, change.uid, change.fields["task_id"],
                                        change.fields["kind"], "{}") == 0
        altered = replace(change, **{
            "table": {"table": "tasks"},
            "uid": {"uid": sync.uuid7()},
            "op": {"op": "delete"},
            "origin": {"origin_gateway": "other"},
            "payload": {"fields": {**change.fields, "payload": "{}"}},
            "board": {},
        }[fault])
        if fault == "board":
            conn.execute("UPDATE kanban_sync_state SET board_id='other'")
        with replica.projection(conn, altered):
            assert permitted(conn, change) == 0


def test_event_permit_scope_is_connection_and_transaction_bound(synced_conn):
    conn = synced_conn
    *_, change = cached(conn)
    assert not conn.in_transaction
    assert permitted(conn, change) == 0
    other = sqlite3.connect(":memory:")
    conn.backup(other)
    try:
        with kb.write_txn(conn), kb.write_txn(other):
            assert permitted(conn, change) == 0
            with replica.projection(conn, change):
                assert permitted(conn, change) == 1
                assert permitted(other, change) == 0
            assert permitted(conn, change) == 0
        # Keep the real scope open while ending its transaction. A stale scope
        # must not become authority merely because its connection still matches.
        conn.execute("BEGIN")
        with replica.projection(conn, change):
            assert permitted(conn, change) == 1
            conn.rollback()
            assert permitted(conn, change) == 0
    finally:
        other.close()


@pytest.mark.parametrize("fault", ["table", "op", "uid", "connection", "transaction"])
def test_task_permit_is_only_exact_task_upsert(synced_conn, fault):
    conn = synced_conn
    target, *_ = fixtures.scenario(conn)
    change = sync.RemoteChange(1, "tasks", target, "upsert", {}, 1, "", "worker")
    other = sqlite3.connect(":memory:")
    try:
        with kb.write_txn(conn), kb.write_txn(other):
            with replica.projection(conn, change):
                assert replica.task_permit(conn, target) == 1
            altered = change
            if fault == "table":
                altered = replace(change, table="task_events")
            elif fault == "op":
                altered = replace(change, op="delete")
            elif fault == "uid":
                altered = replace(change, uid=sync.uuid7())
            with replica.projection(conn, altered):
                if fault == "connection":
                    assert replica.task_permit(other, target) == 0
                elif fault == "transaction":
                    conn.rollback()
                    assert replica.task_permit(conn, target) == 0
                    conn.execute("BEGIN")
                else:
                    assert replica.task_permit(conn, target) == 0
    finally:
        other.close()
    assert replica.task_permit(conn, target) == 0


@pytest.mark.parametrize("fault", ["parent", "child", "table", "op", "connection", "transaction"])
def test_edge_permit_is_only_exact_edge_upsert(synced_conn, fault):
    conn = synced_conn
    target, child, *_ = fixtures.scenario(conn)
    change = sync.RemoteChange(1, "task_links", sync.uuid7(), "upsert",
                               {"parent_id": target, "child_id": child}, 1, "", "worker")
    other = sqlite3.connect(":memory:")
    try:
        with kb.write_txn(conn), kb.write_txn(other):
            with replica.projection(conn, change):
                assert replica.edge_permit(conn, target, child) == 1
            altered = change
            if fault in ("parent", "child"):
                altered = replace(change, fields={**change.fields, fault + "_id": sync.uuid7()})
            elif fault == "table":
                altered = replace(change, table="tasks")
            elif fault == "op":
                altered = replace(change, op="delete")
            with replica.projection(conn, altered):
                if fault == "connection":
                    assert replica.edge_permit(other, target, child) == 0
                elif fault == "transaction":
                    conn.rollback()
                    assert replica.edge_permit(conn, target, child) == 0
                    conn.execute("BEGIN")
                else:
                    assert replica.edge_permit(conn, target, child) == 0
    finally:
        other.close()
    assert replica.edge_permit(conn, target, child) == 0


def test_projection_requires_transaction(synced_conn):
    conn = synced_conn
    *_, change = cached(conn)
    with pytest.raises(ContainmentRefused, match="requires a transaction"):
        with replica.projection(conn, change):
            pytest.fail("nontransactional scope admitted")
    with kb.write_txn(conn), replica.projection(conn, change):
        assert permitted(conn, change) == 1


def test_prepare_apply_no_relevance_does_not_install_guard(monkeypatch):
    from hermes_cli import kanban_containment_guard as guard
    conn = sqlite3.connect(":memory:")
    calls = []
    monkeypatch.setattr(guard, "install", lambda *a, **k: calls.append(a))
    try:
        change = sync.RemoteChange(1, "tasks", "fixture", "upsert", {"priority": 1}, 1, "", "worker")
        replica.prepare_apply(conn, [change])
        assert calls == []
        conn.execute("CREATE TABLE containment_observations(task_id TEXT,episode TEXT)")
        replica.prepare_apply(conn, [change])
        assert len(calls) == 1
    finally:
        conn.close()


def test_prepare_apply_records_cached_release_observation(synced_conn):
    conn = synced_conn
    target, _, _, release, hub, _ = cached(conn)
    change = fixtures.event(release, hub, 2)
    replica.certify_page(conn, hub, "fixture", [change])
    replica.prepare_apply(conn, [change])
    assert tuple(conn.execute("SELECT task_id,episode FROM containment_observations WHERE task_id=?",
                              (target,)).fetchone()) == (target, release["payload"]["episode"])


def test_verified_rejects_differing_cached_boards_and_changed_episode(synced_conn, monkeypatch):
    conn = synced_conn
    _target, _, _stop, release, hub, change = cached(conn)
    replica.certify_page(conn, hub, "fixture", [fixtures.event(release, hub, 2)])
    stop_row = dict(change.fields)
    release_row = dict(hub.events[release["event_uid"]]["row"])
    assert replica.verified(conn, release_row, stop_row) is True
    original = replica._cached
    altered = copy.deepcopy(hub.events[release["event_uid"]])
    altered["board"] = "other"
    monkeypatch.setattr(replica, "_cached", lambda c, u: altered if u == release["event_uid"] else original(c, u))
    assert replica.verified(conn, release_row, stop_row) is False
    monkeypatch.setattr(replica, "_cached", original)
    altered = copy.deepcopy(hub.events[release["event_uid"]])
    import json
    payload = json.loads(altered["row"]["payload"])
    payload["episode"] = sync.uuid7()
    altered["row"]["payload"] = json.dumps(payload)
    # Matching local/cached bytes isolates certified_release from _matches.
    release_row["payload"] = altered["row"]["payload"]
    monkeypatch.setattr(replica, "_cached", lambda c, u: altered if u == release["event_uid"] else original(c, u))
    assert replica.verified(conn, release_row, stop_row) is False


def test_cache_insert_and_update_are_not_raw_authority(synced_conn):
    conn = synced_conn
    *_, change = cached(conn)
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        conn.execute("INSERT INTO containment_canonical_receipts SELECT 'foreign',board,task_id,kind,payload,origin,evidence "
                     "FROM containment_canonical_receipts WHERE uid=?", (change.uid,))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        conn.execute("UPDATE containment_canonical_receipts SET board='other' WHERE uid=?", (change.uid,))


@pytest.mark.parametrize("fault", ["uid", "task_id", "kind", "payload"])
def test_matches_checks_each_local_event_field(synced_conn, fault):
    conn = synced_conn
    *_, hub, change = cached(conn)
    proof = hub.events[change.uid]
    row = dict(change.fields)
    assert replica._matches(row, proof)
    row[fault] = "different"
    assert replica._matches(row, proof) is False


def test_cache_write_udf_requires_exact_connection_and_live_transaction(synced_conn):
    conn = synced_conn
    other = sqlite3.connect(":memory:")
    replica.register(conn)
    replica.register(other)
    def permit(c):
        return c.execute("SELECT kanban_containment_cache_write()").fetchone()[0]
    try:
        with kb.write_txn(conn), kb.write_txn(other):
            assert permit(conn) == 0
            token = replica._CACHE.set(id(conn))
            try:
                assert permit(conn) == 1
                assert permit(other) == 0
                conn.rollback()
                assert permit(conn) == 0
                conn.execute("BEGIN")
            finally:
                replica._CACHE.reset(token)
            assert permit(conn) == 0
    finally:
        other.close()



def test_verified_rejects_consistent_foreign_board_cache(synced_conn):
    import json
    from hermes_cli.kanban_containment_state import fingerprint
    conn = synced_conn
    target, _, stop, release, hub = fixtures.scenario(conn)
    frozen = stop["payload"]
    frozen["target"]["board"] = "other"
    frozen["policy"]["authority"]["board"] = "other"
    release["payload"]["evidence"]["authority"]["board"] = "other"
    release["payload"]["containment_sha256"] = fingerprint(frozen)
    for op in (stop, release):
        proof = hub.events[op["event_uid"]]
        proof["board"] = "other"
        proof["row"]["payload"] = json.dumps(op["payload"])
        proof["containment_v1"]["containment_sha256"] = fingerprint(frozen)
    conn.execute("UPDATE kanban_sync_state SET board_id='other'")
    replica.remember(conn, hub.events[release["event_uid"]], board="other", task=target,
                     uid=release["event_uid"], stop_evidence=hub.events[stop["event_uid"]])
    stop_row = hub.events[stop["event_uid"]]["row"]
    release_row = hub.events[release["event_uid"]]["row"]
    assert replica.verified(conn, release_row, stop_row) is True
    conn.execute("UPDATE kanban_sync_state SET board_id='fixture'")
    assert replica.verified(conn, release_row, stop_row) is False
