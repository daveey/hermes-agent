"""Private consumption proof is historical, never HOME application or clearance."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_release_intent as intent
from hermes_cli import kanban_release_certificate as certificate
from hermes_cli import kanban_release_replica as replica


@pytest.fixture
def proof():
    body = {"version": 1, "intent_uid": "intent-1", "board": "example", "task_id": "t_target",
            "home": "home", "episode": "stop-1", "capability_episodes": ["older-1"], "runs": 2,
            "note": "Continue the original scope", "actor": {"gateway": "origin", "profile": "default", "session": "chat-1"}}
    digest = intent.digest(body)
    op = {"client_op_id": "consume-1", "op": "consume_owner_release", "task": "t_target", "intent_uid": "intent-1",
          "digest": digest, "seen_comments": []}
    payload = {"version": 1, "episode": "stop-1", "covered_restriction_uids": ["restriction-1", "stop-1"],
               "request_uid": "consume-1", "owner_release_intent_uid": "intent-1", "owner_release_digest": digest,
               "actor_kind": "agent", "actor_gateway": "origin", "actor_profile": "default", "actor_session": "chat-1",
               "authority_kind": "chat", "authority_ref": None, "reason": body["note"]}
    event = {"uid": "consume-1", "ord": 3, "task_id": "t_target", "run_uid": None, "kind": "scope_renewed",
             "payload": json.dumps(payload), "created_at": 100, "seq": 10, "version": 1, "updated_at": 100000,
             "hlc": "100000:0:home", "origin_gateway": "home", "deleted_at": None}
    return {"version": 1, "intent": body, "digest": digest, "consume": op, "renewal": event}


def parsed(proof):
    return certificate.validate(proof, board="example", task="t_target", intent_uid="intent-1")


def test_alias_capture_remains_historical_and_exact(proof, store):
    conn, path = store
    # Authenticated Board endpoint established alias-qualified HOME at consumption.
    proof["renewal"]["origin_gateway"] = "renamed-home"
    assert parsed(proof) == proof
    replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as reopened:
        assert replica.verified_consumption(Rows(reopened), "consume-1") == proof
    assert certificate.event_matches(proof["renewal"], proof)
    altered = {**proof["renewal"], "origin_gateway": "newer-home"}
    assert not certificate.event_matches(altered, proof)


@pytest.mark.parametrize("value", [True, 1.0])
def test_event_match_preserves_json_types(proof, value):
    payload = json.loads(proof["renewal"]["payload"])
    payload["version"] = value
    altered = {**proof["renewal"], "payload": json.dumps(payload)}
    assert not certificate.event_matches(altered, proof)
    payload["version"] = 1
    assert certificate.event_matches({**altered, "payload": json.dumps(payload, sort_keys=True)}, proof)


def test_board_emitted_certificate_contract():
    # Actual HTTP/DO private GET, Board b2add7f; captured without changing values.
    proof = json.loads((Path(__file__).parent / "fixtures" / "board_emitted_owner_release_consumption.json").read_text())
    body = proof["intent"]
    assert certificate.validate(proof, board=body["board"], task=body["task_id"], intent_uid=body["intent_uid"]) == proof


def test_real_http_private_get_and_bounded_response(proof, store):
    """Real Hub HTTP -> private cache, using a labelled fixture HTTP peer (not DO)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from hermes_cli.kanban_sync_client import Hub as Client, Identity
    from hermes_cli.kanban_sync_hub_transport import BoundedResponseTooLarge

    received = []
    response = [json.dumps(proof).encode()]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", str(len(response[0])))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(response[0])
            except (ConnectionResetError, BrokenPipeError):
                pass  # bounded client can close before a deliberately oversized body

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn, _ = store
    hub = Client(Identity(url=f"http://127.0.0.1:{server.server_port}", key="fixture-key", gateway="home"))
    try:
        assert replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1") == proof
        assert received == [("/v2/boards/example/owner-release/intent-1/consumption", "Bearer fixture-key")]
        response[0] = b' ' * (certificate.MAX_BYTES + 1)
        with pytest.raises(BoundedResponseTooLarge):
            hub.owner_release_consumption("example", "intent-1")
        assert replica.verified_consumption(conn, "consume-1") == proof
    finally:
        hub.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_exact_certificate_and_historical_identity(proof):
    assert parsed(proof) == proof
    # No mutable task/owner/clock input: expiry and current ownership are application gates.
    assert certificate.event_matches({**proof["renewal"], "id": 52, "run_id": None}, proof)
    assert certificate.coverage(proof) == frozenset({"restriction-1", "stop-1"})


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("version",), 2), (("extra",), True),
    (("intent", "version"), True), (("intent", "board"), "other"),
    (("intent", "task_id"), "t_other"), (("intent", "intent_uid"), "other"),
    (("digest",), "a" * 64), (("consume", "op"), "other"),
    (("consume", "client_op_id"), "other"), (("consume", "task"), "t_other"),
    (("consume", "intent_uid"), "other"), (("consume", "digest"), "a" * 64),
    (("consume", "extra"), True), (("consume", "seen_comments"), {}),
    (("renewal", "uid"), "other"), (("renewal", "task_id"), "t_other"),
    (("renewal", "kind"), "commented"), (("renewal", "run_uid"), "run-1"),
    (("renewal", "origin_gateway"), ""), (("renewal", "deleted_at"), 1),
    (("renewal", "version"), 2), (("renewal", "ord"), True),
    (("renewal", "payload"), "[]"), (("renewal", "extra"), True),
])
def test_reject_inconsistent_half(proof, path, value):
    target = proof
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        parsed(proof)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("episode", "other"), ("request_uid", "other"),
    ("owner_release_intent_uid", "other"), ("owner_release_digest", "a" * 64),
    ("actor_kind", "human"), ("actor_gateway", "other"), ("actor_profile", "other"),
    ("actor_session", "other"), ("authority_kind", "human"), ("authority_ref", "grant"),
    ("reason", "different"), ("covered_restriction_uids", []),
    ("covered_restriction_uids", ["stop-1", "stop-1"]),
    ("covered_restriction_uids", ["other"]), ("covered_restriction_uids", [None, "stop-1"]),
    ("covered_restriction_uids", ["stop-1", "restriction-1"]),
    ("mandate_comment_uid", "forged-comment"),
])
def test_reject_payload_bindings(proof, field, value):
    payload = json.loads(proof["renewal"]["payload"])
    payload[field] = value
    proof["renewal"]["payload"] = json.dumps(payload)
    with pytest.raises(ValueError):
        parsed(proof)


@pytest.mark.parametrize("field,value", [("board", "other"), ("task", "t_other"), ("intent_uid", "other")])
def test_external_lookup_coordinates_are_bound(proof, field, value):
    coordinates = {"board": "example", "task": "t_target", "intent_uid": "intent-1"}
    coordinates[field] = value
    with pytest.raises(ValueError):
        certificate.validate(proof, **coordinates)


@pytest.mark.parametrize("field,value", [("task", "t_other"), ("intent_uid", "other")])
def test_intent_lookup_binding_with_other_halves_coherent(proof, field, value):
    coordinates = {"board": "example", "task": "t_target", "intent_uid": "intent-1"}
    coordinates[field] = value
    if field == "task":
        proof["consume"]["task"] = proof["renewal"]["task_id"] = value
    else:
        proof["consume"]["intent_uid"] = value
    with pytest.raises(ValueError):
        certificate.validate(proof, **coordinates)


def test_digest_must_bind_intent_even_when_every_other_half_agrees(proof):
    # Keep adjacent digest comparisons satisfied so only real intent hashing rejects.
    proof["digest"] = proof["consume"]["digest"] = "a" * 64
    payload = json.loads(proof["renewal"]["payload"])
    payload["owner_release_digest"] = proof["digest"]
    proof["renewal"]["payload"] = json.dumps(payload)
    with pytest.raises(ValueError):
        parsed(proof)


def test_byte_bounds_duplicate_keys_and_nonfinite(proof):
    raw = json.dumps(proof)
    assert certificate.parse(raw) == proof
    for bad in ('{"version":1,"version":1}', '{"n":NaN}', '[]', b'{}', '"' + 'x' * certificate.MAX_BYTES + '"'):
        with pytest.raises(ValueError):
            certificate.parse(bad)


class Rows:
    """The actionability adapter is deliberately not a sqlite3.Connection."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "private.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE kanban_sync_state(board_id TEXT)")
    conn.execute("INSERT INTO kanban_sync_state VALUES ('example')")
    yield conn, path
    conn.close()


class Hub:
    def __init__(self, proof):
        self.proof = proof
        self.ident = SimpleNamespace(gateway="home")
        self.calls = []

    def owner_release_consumption(self, board, uid):
        self.calls.append((board, uid))
        return copy.deepcopy(self.proof)


def test_certified_cache_readonly_reopen_no_coverage_authority(proof, store):
    conn, path = store
    hub = Hub(proof)
    assert replica.verified_consumption(Rows(conn), "consume-1") is None
    assert replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1") == proof
    assert hub.calls == [("example", "intent-1")]
    assert replica.verified_consumption(Rows(conn), "consume-1") == proof
    assert replica.verified_consumption(Rows(conn), "missing") is None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as reopened:
        reopened.execute("PRAGMA query_only=ON")
        assert replica.verified_consumption(Rows(reopened), "consume-1") == proof
    # This cache never changes scope_renewal, tasks, events or execution permission.
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == [
        ("kanban_sync_state",), (replica.TABLE,)]


def test_cache_conflict_replay_and_all_keys_immutable(proof, store):
    conn, _ = store
    hub = Hub(proof)
    replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1")
    before = conn.execute(f"SELECT * FROM {replica.TABLE}").fetchall()
    replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1")
    assert conn.execute(f"SELECT * FROM {replica.TABLE}").fetchall() == before
    for sql in (f"DELETE FROM {replica.TABLE}", f"UPDATE {replica.TABLE} SET evidence='{{}}'",
                f"INSERT OR REPLACE INTO {replica.TABLE} SELECT * FROM {replica.TABLE}"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    assert conn.execute(f"SELECT * FROM {replica.TABLE}").fetchall() == before
    hub.proof["renewal"]["seq"] += 1
    with pytest.raises(ValueError, match="conflict"):
        replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1")
    assert conn.execute(f"SELECT * FROM {replica.TABLE}").fetchall() == before


def test_no_fetch_in_writer_no_cache_on_invalid_or_unavailable(proof, store):
    conn, _ = store
    hub = Hub(proof)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="transaction"):
        replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1")
    conn.rollback()
    assert hub.calls == []
    hub.proof["digest"] = "a" * 64
    with pytest.raises(ValueError):
        replica.certify(conn, hub, "example", task="t_target", intent_uid="intent-1")
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (replica.TABLE,)).fetchone()


def test_foreign_board_and_corrupt_cached_identity_failclosed(proof, store):
    conn, _ = store
    replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    conn.execute("UPDATE kanban_sync_state SET board_id='other'")
    assert replica.verified_consumption(Rows(conn), "consume-1") is None
    with pytest.raises(ValueError, match="board"):
        replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")


def _comment():
    return {"uid": "comment-1", "task_id": "t_target", "author": "default", "body": "note",
            "created_at": 99, "seq": 5, "version": 1, "updated_at": 99000,
            "hlc": "99000:0:home", "origin_gateway": "home", "deleted_at": None}


@pytest.mark.parametrize("field,value", [
    ("task_id", "other"), ("uid", ""), ("deleted_at", 1), ("author", None),
    ("body", None), ("hlc", None), ("origin_gateway", ""), ("seq", True),
    ("version", 0), ("created_at", -1), ("updated_at", 1.0), ("extra", True),
])
def test_comment_certificate_shapes(proof, field, value):
    comment = _comment()
    comment[field] = value
    proof["consume"]["seen_comments"] = [comment]
    with pytest.raises(ValueError):
        parsed(proof)


def test_comments_duplicates_bounds_and_lossless_cache(proof, store):
    conn, _ = store
    first, second = _comment(), {**_comment(), "uid": "comment-2"}
    first["body"] = "Unicode \U0001f600 and old escaped surrogate \ud800"
    proof["consume"]["seen_comments"] = [first, second]
    assert parsed(proof) == proof
    replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    assert replica.verified_consumption(Rows(conn), "consume-1") == proof
    for rows in ([first, first], [second, first], [first] * 513):
        proof["consume"]["seen_comments"] = rows
        with pytest.raises(ValueError):
            parsed(proof)


def test_replace_guard_is_independent_of_insertion_permit(proof, store):
    conn, _ = store
    replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    token = replica._PERMIT.set(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Exercise every key independently, with adjacent writer guard satisfied.
        for column, replacement in (("consume_uid", "other-consume"), ("intent_uid", "other-intent")):
            columns = ["consume_uid", "intent_uid", "board", "task_id", "digest", "evidence"]
            projected = ["?" if name == column else name for name in columns]
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(f"INSERT OR REPLACE INTO {replica.TABLE} SELECT " + ",".join(projected) + f" FROM {replica.TABLE}", (replacement,))
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(f"INSERT OR REPLACE INTO {replica.TABLE}(rowid,consume_uid,intent_uid,board,task_id,digest,evidence) "
                         f"SELECT rowid,'other-consume','other-intent',board,task_id,digest,evidence FROM {replica.TABLE}")
    finally:
        conn.rollback()
        replica._PERMIT.reset(token)


@pytest.mark.parametrize("field,value", [("consume_uid", "other"), ("intent_uid", "other"), ("digest", "a" * 64),
                                        ("task_id", "other"), ("evidence", "{}"), ("evidence", b"{}")])
def test_corrupt_optional_cache_fails_closed(proof, store, field, value):
    conn, _ = store
    replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    # Explicit corruption fixture bypasses only immutability, never certification.
    conn.execute(f"DROP TRIGGER {replica.TABLE}_update")
    conn.execute(f"UPDATE {replica.TABLE} SET {field}=?", (value,))
    uid = value if field == "consume_uid" else "consume-1"
    assert replica.verified_consumption(Rows(conn), uid) is None


def test_postinsert_failure_rolls_back_complete_cache(proof, store):
    conn, _ = store
    replica.install(conn)
    conn.execute(f"CREATE TRIGGER injected AFTER INSERT ON {replica.TABLE} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        replica.certify(conn, Hub(proof), "example", task="t_target", intent_uid="intent-1")
    assert conn.execute(f"SELECT * FROM {replica.TABLE}").fetchall() == []
