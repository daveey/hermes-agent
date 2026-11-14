import dataclasses
import json
import sqlite3

import pytest

from hermes_cli import kanban_annotation_ledger as L
from hermes_cli import kanban_origin_ledger as O


def _binding(task="t1", origin=("gw", "p", "s1"), owner=("ogw", "op", "board"), bid="b1"):
    return {"binding_id": bid, "version": 1,
            "owner": {"gateway": owner[0], "profile": owner[1], "board": owner[2], "task_id": task},
            "origin": {"gateway": origin[0], "profile": origin[1], "session": origin[2]},
            "task_record_key": f"{owner[1]}/{owner[2]}/{task}", "publication_version": 1}


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "l.db", isolation_level=None)
    c.execute("CREATE TABLE origin_obligations (binding_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL,"
              "binding_json TEXT NOT NULL, resolved_session TEXT NOT NULL, task_json TEXT NOT NULL,"
              "observation_version INTEGER NOT NULL, released INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE TABLE origin_receipts (request_id TEXT PRIMARY KEY NOT NULL, binding_hash TEXT NOT NULL,"
              "receipt_json TEXT NOT NULL)")
    return c


def _add(c, b, *, released=0, task='{"a": 1}', version=3, rid=None, receipt_hash=None, col_hash=None):
    h = O.binding_hash(b)
    c.execute("INSERT INTO origin_obligations VALUES (?,?,?,?,?,?,?)",
              (O._binding_key(b), col_hash or h, O._json(b), "res", task, version, released))
    if rid:
        c.execute("INSERT INTO origin_receipts VALUES (?,?,?)",
                  (rid, col_hash or h, json.dumps({"binding_hash": receipt_hash or h, "status": "accepted"})))


def _snap(c, gw="gw", p="p"):
    c.execute("BEGIN")
    try:
        return L.snapshot(c, gw, p)
    finally:
        c.rollback()


def test_requires_read_transaction(tmp_path):
    c = _conn(tmp_path)
    with pytest.raises(ValueError, match="origin_ledger_snapshot_required"):
        L.snapshot(c, "gw", "p")


def test_missing_tables_fail_closed_and_no_schema_created(tmp_path):
    c = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _snap(c)
    assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["origin_obligations", "origin_receipts"])
def test_partial_schema_fails_closed(tmp_path, table):
    c = _conn(tmp_path)
    _add(c, _binding(), rid="req-00000001")
    c.execute(f"DROP TABLE {table}")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _snap(c)


def test_filters_origin_and_keeps_released_raw(tmp_path):
    c = _conn(tmp_path)
    _add(c, _binding("t1"), released=1, rid="req-00000001", task='{ "z":1 }')
    _add(c, _binding("t2", bid="b2"), released=0)
    _add(c, _binding("t3", origin=("other", "p", "s"), bid="b3"), rid="req-00000003")
    _add(c, _binding("t4", origin=("gw", "q", "s"), bid="b4"))
    s = _snap(c)
    assert [o["binding"]["owner"]["task_id"] for o in s.obligations] == ["t1", "t2"]
    first = s.obligations[0]
    assert first["released"] == 1 and first["task_json"] == '{ "z":1 }' and first["observation_version"] == 3
    assert [r["request_id"] for r in s.receipts] == ["req-00000001"]
    assert s.owner_keys == (("ogw", "op/board/t1"), ("ogw", "op/board/t2"))
    assert s.own_boards == ()


def test_receipt_links_by_parsed_binding_hash_not_sql_column(tmp_path):
    c = _conn(tmp_path)
    b = _binding("t1")
    _add(c, b, rid="req-00000001")
    # SQL column points to the relevant hash but parsed receipt says foreign: excluded.
    c.execute("INSERT INTO origin_receipts VALUES (?,?,?)",
              ("req-00000002", O.binding_hash(b), json.dumps({"binding_hash": "f" * 64})))
    # SQL column foreign but parsed hash relevant: included.
    c.execute("INSERT INTO origin_receipts VALUES (?,?,?)",
              ("req-00000003", "f" * 64, json.dumps({"binding_hash": O.binding_hash(b)})))
    s = _snap(c)
    assert [r["request_id"] for r in s.receipts] == ["req-00000001", "req-00000003"]
    assert s.receipts[1]["binding_hash"] == "f" * 64  # raw column preserved


def test_local_owner_populates_own_boards(tmp_path):
    c = _conn(tmp_path)
    _add(c, _binding("t1", owner=("gw", "p", "main")))
    _add(c, _binding("t2", owner=("gw", "p", "alt"), bid="b2"))
    _add(c, _binding("t3", owner=("gw", "x", "nope"), bid="b3"))
    assert _snap(c).own_boards == (("p", "alt"), ("p", "main"))


def test_token_deterministic_independent_of_insert_order(tmp_path):
    bs = [_binding(f"t{i}", bid=f"b{i}") for i in range(3)]
    c1 = _conn(tmp_path)
    for i, b in enumerate(bs):
        _add(c1, b, rid=f"req-0000000{i}")
    c2 = sqlite3.connect(":memory:", isolation_level=None)
    for (sql,) in c1.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
        c2.execute(sql)
    for i, b in reversed(list(enumerate(bs))):
        _add(c2, b, rid=f"req-0000000{i}")
    assert _snap(c1).token == _snap(c2).token


def test_token_changes_on_any_relevant_column(tmp_path):
    c = _conn(tmp_path)
    b = _binding()
    _add(c, b, rid="req-00000001")
    before = _snap(c).token
    for sql in ("UPDATE origin_obligations SET released=1", "UPDATE origin_obligations SET observation_version=9",
                "UPDATE origin_obligations SET task_json='{\"a\":1}'",
                "UPDATE origin_obligations SET resolved_session='zz'",
                "UPDATE origin_receipts SET receipt_json=receipt_json||' '"):
        c.execute(sql)
        after = _snap(c).token
        assert after != before, sql
        before = after
    # foreign rows do not move the token
    _add(c, _binding("t9", origin=("o", "o", "o"), bid="b9"), rid="req-00000009")
    assert _snap(c).token == before


def test_immutable(tmp_path):
    c = _conn(tmp_path)
    _add(c, _binding(), rid="req-00000001")
    s = _snap(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.token = "x"
    with pytest.raises(TypeError):
        s.obligations[0]["released"] = 5
    with pytest.raises(TypeError):
        s.obligations[0]["binding"]["owner"]["task_id"] = "x"
    assert isinstance(s.obligations, tuple) and isinstance(s.owner_keys, tuple)
    c.execute("UPDATE origin_obligations SET released=1")
    assert s.obligations[0]["released"] == 0


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("owner"),
    lambda b: b["origin"].update(session="bad session"),
    lambda b: b.update(task_record_key="wrong"),
])
def test_malformed_binding_fails_closed_even_foreign(tmp_path, mutate):
    c = _conn(tmp_path)
    b = _binding(origin=("other", "p", "s"))
    _add(c, b)
    bad = json.loads(O._json(b))
    mutate(bad)
    c.execute("UPDATE origin_obligations SET binding_json=?", (json.dumps(bad),))
    with pytest.raises(ValueError, match="origin_binding_invalid"):
        _snap(c)


def test_non_json_binding_and_receipt_fail_closed(tmp_path):
    c = _conn(tmp_path)
    _add(c, _binding())
    c.execute("INSERT INTO origin_receipts VALUES ('req-00000001','h','not json')")
    with pytest.raises(ValueError, match="origin_receipt_invalid"):
        _snap(c)
    c.execute("DELETE FROM origin_receipts")
    c.execute("UPDATE origin_obligations SET binding_json='{'")
    with pytest.raises(ValueError, match="origin_binding_invalid"):
        _snap(c)


def test_key_and_hash_mismatch_fail_closed(tmp_path):
    c = _conn(tmp_path)
    _add(c, _binding(), col_hash="a" * 64)
    with pytest.raises(ValueError, match="origin_obligation_inconsistent"):
        _snap(c)
    c.execute("DELETE FROM origin_obligations")
    _add(c, _binding())
    c.execute("UPDATE origin_obligations SET binding_id='wrong'")
    with pytest.raises(ValueError, match="origin_obligation_inconsistent"):
        _snap(c)


def test_bad_args(tmp_path):
    c = _conn(tmp_path)
    c.execute("BEGIN")
    with pytest.raises(ValueError, match="origin_scope_invalid"):
        L.snapshot(c, "", "p")
