"""Creation evidence follows the real insert, transaction and sealed reader."""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_creation as creation
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli import kanban_fairness_store as store


@pytest.fixture
def capture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    config = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "creation-test",
                                            "directory": str(tmp_path / "runtime")}}}
    with kbc.connect_closing() as conn:
        journal.arm(conn, "creation-test")
        session = runtime.activate(config, home=home, board="default", source_gateway="test", board_uid="board")
        try:
            yield conn, session
        finally:
            session.close()


def test_explicit_zero_survives_to_sealed_creation_receipt(capture, tmp_path):
    conn, _session = capture
    parent = kb.create_task(conn, title="parent", assignee="a", priority=900)
    evidence = {}
    args = creation.inherit_tool_priority(conn, {"priority": 0}, parent, receipt=evidence)
    child = kb.create_task(conn, title="child", assignee="a", creator_task_id=parent,
                           priority=args["priority"], creation_receipt=evidence)

    out = tmp_path / "journal"
    store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    loaded = store.load(out)
    receipts = [r for r in loaded["records"] if r["kind"] == "creation" and r["payload"]["task_uid"] == child]
    assert len(receipts) == 1, (evidence, list(journal.rows(conn)), loaded["gaps"], _session.abandoned.is_set())
    payload = receipts[0]["payload"]
    assert (payload["priority_intent"], payload["priority_argument"], payload["stored_child_priority"]) == ("explicit", 0, 0)
    assert payload["creator_task_uid"] == parent
    assert not {"journal_sequence_gap", "record_count", "export_failed"} & {g["code"] for g in loaded["gaps"]}


def test_disabled_receipt_has_no_provenance_reads(capture, monkeypatch):
    from hermes_cli import kanban_fairness_creation as receipts

    conn, session = capture
    session.close()
    monkeypatch.setattr(receipts, "_payload", lambda *a: pytest.fail("disabled provenance read"))
    monkeypatch.setattr(receipts, "_append", lambda *a: pytest.fail("disabled journal read"))
    evidence = {}
    original = {"priority": 0}
    assert creation.inherit_tool_priority(conn, original, "missing", receipt=evidence) is original
    assert evidence == {}
    child = kb.create_task(conn, title="disabled", assignee="a", priority=0)
    assert kb.get_task(conn, child).priority == 0
    assert not [r for r in journal.rows(conn) if r[1] == "creation"]


@pytest.mark.parametrize("argument,intent,expected", [("omitted", "omitted", 1000), (None, "explicit", 1000), (0, "explicit", 0)])
def test_donated_priority_cap_and_missing_provenance(capture, argument, intent, expected):
    import json

    conn, _session = capture
    parent = kb.create_task(conn, title="parent", assignee="a", admission_class="urgent")
    descendant = kb.create_task(conn, title="descendant", assignee="a", parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority=60000 WHERE id=?", (descendant,))
    evidence = {}
    raw = {} if argument == "omitted" else {"priority": argument}
    args = creation.inherit_tool_priority(conn, raw, parent, receipt=evidence)
    child = kb.create_task(conn, title="child", assignee="a", creator_task_id=parent,
                           priority=args["priority"], creation_receipt=evidence)
    payload = next(json.loads(r[5]) for r in journal.rows(conn) if r[1] == "creation" and r[3] == child)
    assert payload["priority_intent"] == intent
    assert payload["priority_argument"] == (None if argument == "omitted" else argument)
    assert payload["effective_parent_priority"] == payload["inheritance_cap"] == 1000
    assert payload["stored_child_priority"] == expected
    for name in ("creator_episode_uid", "parent_snapshot_cut", "producer_source_hash"):
        assert payload[name] is None and name in payload["unknown_fields"]


def test_missing_parent_is_not_reported_as_known_zero(capture):
    import json

    conn, _session = capture
    evidence = {}
    args = creation.inherit_tool_priority(conn, {}, "absent", receipt=evidence)
    assert args["priority"] == 0
    child = kb.create_task(conn, title="child", assignee="a", creator_task_id="absent",
                           priority=0, creation_receipt=evidence)
    payload = next(json.loads(r[5]) for r in journal.rows(conn) if r[1] == "creation" and r[3] == child)
    assert payload["effective_parent_priority"] is None
    assert "effective_parent_priority" in payload["unknown_fields"]


def test_outer_rollback_and_inner_savepoint_rollback_remove_receipts(capture):
    conn, _session = capture
    before = list(journal.rows(conn))
    counts = journal.status(conn)
    with pytest.raises(RuntimeError, match="outer"):
        with kb.write_txn(conn):
            kb.create_task(conn, title="outer-aborted", assignee="a")
            raise RuntimeError("outer")
    assert list(journal.rows(conn)) == before
    assert journal.status(conn)["stored"] == counts["stored"]
    with kb.write_txn(conn):
        with pytest.raises(RuntimeError, match="inner"):
            with kb.write_txn(conn, allow_nested=True):
                kb.create_task(conn, title="inner-aborted", assignee="a")
                raise RuntimeError("inner")
        kept = kb.create_task(conn, title="kept", assignee="a")
    assert [r[3] for r in journal.rows(conn) if r[1] == "creation"] == [kept]


@pytest.mark.parametrize("limit", ["records", "bytes"])
def test_journal_bound_counts_creation_loss_without_refusing_child(capture, limit):
    conn, _session = capture
    with kb.write_txn(conn):
        column = {"records": "max_records", "bytes": "max_bytes"}[limit]
        conn.execute(f"UPDATE {journal.SESSION} SET {column}=0")
    child = kb.create_task(conn, title="bounded", assignee="a", priority=23)
    assert kb.get_task(conn, child).priority == 23
    assert not list(journal.rows(conn))
    assert journal.status(conn)["dropped"] >= 3  # task, created event, creation receipt


@pytest.mark.parametrize("custody", ["missing", "sealed", "mismatched"])
def test_missing_journal_custody_invalidates_capture_not_creation(capture, custody):
    conn, session = capture
    if custody == "missing":
        journal.clear(conn)
    elif custody == "sealed":
        journal.seal(conn)
    else:
        with kb.write_txn(conn):
            conn.execute(f"UPDATE {journal.SESSION} SET capture_id='different'")
    child = kb.create_task(conn, title="unobserved", assignee="a", priority=19)
    assert kb.get_task(conn, child).priority == 19
    assert session.abandoned.is_set()


def test_oversize_original_scalar_does_not_expand_journal(capture):
    conn, session = capture
    evidence = dict(capture_id="creation-test", creator_task_uid=None, priority_intent="explicit",
                    priority_argument="0" * (runtime.MAX_PAYLOAD_BYTES + 1))
    child = kb.create_task(conn, title="bounded original", assignee="a", priority=0, creation_receipt=evidence)
    assert kb.get_task(conn, child).priority == 0
    assert session.abandoned.is_set()
    assert not [r for r in journal.rows(conn) if r[1] == "creation"]


def test_explicit_priority_observation_failure_preserves_original_behavior(capture, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_admission_score

    conn, session = capture
    def unavailable(*args):
        raise sqlite3.OperationalError("fixture observation unavailable")
    monkeypatch.setattr(kanban_admission_score, "effective_priorities", unavailable)
    original = {"priority": 0}
    assert creation.inherit_tool_priority(conn, original, "parent", receipt={}) is original
    assert session.abandoned.is_set()


def test_receipt_freezes_parent_read_and_observes_stored_lane_result(capture):
    import json

    conn, _session = capture
    parent = kb.create_task(conn, title="parent", assignee="a", priority=37)
    evidence = {}
    creation.inherit_tool_priority(conn, {}, parent, receipt=evidence)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority=800 WHERE id=?", (parent,))
    child = kb.create_task(conn, title="stored", assignee="a", creator_task_id=parent,
                           priority=60000, creation_receipt=evidence)
    payload = next(json.loads(r[5]) for r in journal.rows(conn) if r[1] == "creation" and r[3] == child)
    assert payload["effective_parent_priority"] == 37
    assert payload["stored_child_priority"] == kb.get_task(conn, child).priority == 1000


def test_direct_producer_does_not_infer_original_intent(capture):
    import json

    conn, _session = capture
    child = kb.create_task(conn, title="direct", assignee="a", priority=0)
    payload = next(json.loads(r[5]) for r in journal.rows(conn) if r[1] == "creation" and r[3] == child)
    assert payload["priority_intent"] == "unknown"
    assert {"priority_intent", "priority_argument", "creator_task_uid"} <= set(payload["unknown_fields"])


def test_abandoned_capture_does_not_retry_observation(capture, monkeypatch):
    from hermes_cli import kanban_fairness_creation as receipts

    conn, session = capture
    runtime.invalidate(board="default")
    monkeypatch.setattr(receipts, "_payload", lambda *a: pytest.fail("retried invalid capture"))
    child = kb.create_task(conn, title="after invalidation", assignee="a")
    assert kb.get_task(conn, child) is not None
    assert session.abandoned.is_set()


def test_named_board_uses_connection_not_default(capture, tmp_path):
    _conn, default_session = capture
    kb.create_board("work")
    kb.set_current_board("work")
    home = Path(os.environ["HERMES_HOME"])
    config = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "named-test",
                                            "directory": str(tmp_path / "named")}}}
    named = runtime.activate(config, home=home, board="work", source_gateway="test", board_uid="work")
    try:
        with kbc.connect_closing() as other:
            journal.arm(other, "named-test")
            child = kb.create_task(other, title="named", assignee="a")
            assert [r[3] for r in journal.rows(other) if r[1] == "creation"] == [child]
        assert not default_session.abandoned.is_set()
    finally:
        named.close()


def test_sqlite_auto_rollback_does_not_commit_orphan_changes(capture, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_fairness_creation as receipts

    conn, session = capture
    before = [tuple(row) for row in conn.execute("SELECT * FROM kanban_task_change ORDER BY task_id")]

    calls = []

    def auto_rollback(conn, *args):
        calls.append(conn.in_transaction)
        conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("simulated SQLite auto-rollback")

    monkeypatch.setattr(receipts, "_append", auto_rollback)
    with pytest.raises(sqlite3.OperationalError, match="simulated SQLite auto-rollback"):
        kb.create_task(conn, title="auto-rollback child", assignee="a")
    assert calls == [True]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert [tuple(row) for row in conn.execute("SELECT * FROM kanban_task_change ORDER BY task_id")] == before
    assert session.abandoned.is_set()


@pytest.mark.parametrize("mismatch", ["creator_task_uid", "capture_id"])
def test_receipt_custody_mismatch_abandons_without_record(capture, mismatch):
    conn, session = capture
    evidence = {}
    creation.inherit_tool_priority(conn, {"priority": 5}, None, receipt=evidence)
    evidence[mismatch] = "different-custodian"
    child = kb.create_task(conn, title="mismatched receipt", assignee="a", priority=5,
                           creation_receipt=evidence)
    assert kb.get_task(conn, child).priority == 5
    assert session.abandoned.is_set()
    assert not [row for row in journal.rows(conn) if row[1] == "creation"]


def test_origin_retry_excludes_internal_observation(capture):
    from tests.hermes_cli.test_kanban_origin_creation import intent

    conn, _session = capture
    child = kb.create_task(conn, title="origin", origin_intent=intent())
    assert kb.create_task(conn, title="origin", origin_intent=intent(), creation_receipt={"internal": True}) == child
    raw = conn.execute("SELECT intent_json FROM origin_creation_intents").fetchone()[0]
    assert "creation_receipt" not in raw



