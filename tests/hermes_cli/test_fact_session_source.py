"""Real session writers through the native source adapter, not FakeStore. [daveey]"""
import threading

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_session_source import SessionContentSource, capture_sessions
from hermes_cli.fact_store import FactStore
from tests.hermes_state import test_session_change_cursor as helpers

store = helpers.store


def rows(conn, ids):
    sql = "SELECT id,title,input_tokens,transcript_revision FROM sessions WHERE id IN (" + ",".join("?" * len(ids)) + ")"
    return (dict(zip(("id", "title", "input_tokens", "transcript_revision"), row))
            for row in conn.execute(sql, ids))


@pytest.mark.platforms("linux")
def test_native_session_writer_to_atomic_store(store):
    db, reader, path = store
    db.create_session("a", "cli")
    scope = Scope("install", "profile")
    facts = FactStore()
    condition = threading.Condition()
    errors = []
    passes = []
    membership = set()
    def key(identity):
        return FactKey(scope, "SessionContentInput", identity)
    def commit(adapter, capture):
        nonlocal membership
        passes.append(capture)
        present = {row["id"] for row in capture.records}
        missing = (membership - present) if capture.full else (set(capture.ids) - present)
        facts.publish([Fact(key(row["id"]), row, health="fresh") for row in capture.records],
                      deleted_keys=[key(identity) for identity in missing])
        membership = (membership - missing) | present
        with condition:
            condition.notify_all()
    def failure(adapter, error):
        with condition:
            errors.append(error)
            condition.notify_all()
    def until(predicate):
        with condition:
            assert condition.wait_for(predicate, timeout=10), (errors, passes, db.db_path,
                reader.execute("SELECT id,input_tokens FROM sessions").fetchall())
    feed = ChangeFeed(commit, failure, budget=CaptureBudget(max_seconds=2))
    try:
        feed.register(SessionContentSource(SourceId(scope, "sessions.content", "local"), path, rows))
        until(lambda: facts.snapshot(scope).get(key("a")) is not None)
        initial = facts.snapshot(scope)
        db._execute_write(lambda c: c.execute("UPDATE sessions SET input_tokens=13 WHERE id='a'"))
        until(lambda: facts.snapshot(scope).get(key("a")).value["input_tokens"] == 13)
        db._execute_write(lambda c: c.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('a','user','hello',1)"))
        before = reader.execute("SELECT transcript_revision FROM sessions WHERE id='a'").fetchone()[0]
        db._execute_write(lambda c: c.execute("UPDATE messages SET content='edited' WHERE session_id='a'"))
        until(lambda: facts.snapshot(scope).get(key("a")).value["transcript_revision"] > before)
        assert initial.get(key("a")).value["input_tokens"] != 13
        db.delete_session("a")
        until(lambda: facts.snapshot(scope).get(key("a")) is None)
        assert not errors
    finally:
        feed.close()


def test_session_rows_and_both_cursors_share_transaction(store):
    db, _reader, path = store
    db.create_session("a", "cli")
    budget = CaptureBudget(max_seconds=2)
    def interleaved(conn, ids):
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        db._execute_write(lambda c: c.execute("UPDATE sessions SET input_tokens=27 WHERE id='a'"))
        return rows(conn, ids)
    first = capture_sessions(path, None, interleaved, budget=budget)
    assert first.records[0]["input_tokens"] != 27
    second = capture_sessions(path, first.after, rows, budget=budget)
    assert second.ids == ("a",)
    assert second.records[0]["input_tokens"] == 27
