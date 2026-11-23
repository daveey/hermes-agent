"""Message edits and counters have their own source clock, not a list clock. [daveey]"""
import pytest

import hermes_state_session_change as listing
from tests.hermes_state import test_session_change_cursor as helpers

store = helpers.store


def content():
    import hermes_state_session_content_change
    return hermes_state_session_content_change


def test_writer_installs_content_clock_without_changing_list_clock(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    _, before = content().changed_since(reader, None)
    assert before is not None
    _, legacy = listing.changed_since(reader, None)
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET output_tokens=41, transcript_revision=2 WHERE id='a'"))
    assert content().changed_since(reader, before)[0] == {"a"}
    assert listing.changed_since(reader, legacy)[0] == frozenset()


@pytest.mark.parametrize("mutation,expected", [
    ("UPDATE messages SET content='edited' WHERE id=1", {"a"}),
    ("UPDATE messages SET timestamp=23 WHERE id=1", {"a"}),
    ("UPDATE messages SET session_id='b' WHERE id=1", {"a", "b"}),
    ("DELETE FROM messages WHERE id=1", {"a"}),
    # Existing transcript triggers bump transcript_revision even for this SQL
    # no-op; the content stream must report that real counter mutation.
    ("UPDATE messages SET content=content WHERE id=1", {"a"}),
    ("UPDATE sessions SET output_tokens=output_tokens WHERE id='a'", set()),
])
def test_message_mutation_matrix(store, mutation, expected):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    _, before = content().changed_since(reader, None)
    db._execute_write(lambda c: c.execute(
        "INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (1,'a','user','hello',1)"))
    dirty, before = content().changed_since(reader, before)
    assert dirty == {"a"}
    db._execute_write(lambda c: c.execute(mutation))
    assert content().changed_since(reader, before)[0] == expected


def test_missing_trigger_repair_rotates_epoch_and_preserves_last_cursor(store):
    db, reader, _ = store
    _, before = content().changed_since(reader, None)
    db._execute_write(lambda c: c.execute("DROP TRIGGER session_content_on_messages_update"))
    assert content().changed_since(reader, before) == (content().FULL, None)
    db._execute_write(lambda c: content().ensure_installed(c))
    dirty, repaired = content().changed_since(reader, before)
    assert dirty is content().FULL
    assert repaired.epoch != before.epoch
    total = reader.total_changes
    assert not content().ensure_installed(reader)
    assert reader.total_changes == total


def test_bounded_dirty_read_and_regressed_revision(store):
    db, reader, _ = store
    for key in ("a", "b", "c"):
        db.create_session(key, "cli")
    _, before = content().changed_since(reader, None)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET input_tokens=3"))
    dirty, after = content().changed_since(reader, before, limit=2)
    assert dirty is content().FULL
    assert content().changed_since(reader, after)[0] == set()
    db._execute_write(lambda c: c.execute("UPDATE session_content_revision SET revision=0"))
    assert content().changed_since(reader, after) == (content().FULL, None)


@pytest.mark.parametrize("column", sorted(listing.COUNTER_COLUMNS))
def test_each_counter_marks_its_session(store, column):
    db, reader, _ = store
    db.create_session("a", "cli")
    _, before = content().changed_since(reader, None)
    db._execute_write(lambda c: c.execute(f'UPDATE sessions SET "{column}"=7 WHERE id=\'a\''))
    assert content().changed_since(reader, before)[0] == {"a"}


def test_session_rekey_and_delete_keep_old_keys(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    _, before = content().changed_since(reader, None)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET id='b' WHERE id='a'"))
    dirty, before = content().changed_since(reader, before)
    assert dirty == {"a", "b"}
    db.delete_session("b")
    assert content().changed_since(reader, before)[0] == {"b"}
    assert reader.execute("SELECT id FROM sessions").fetchall() == []


@pytest.mark.parametrize("replacement", [None, "(session_id)", "(rev) WHERE rev>10"])
def test_missing_or_wrong_revision_index_requires_repair(store, replacement):
    db, reader, _ = store
    _, before = content().changed_since(reader, None)
    def break_index(conn):
        conn.execute("DROP INDEX idx_session_content_change_rev")
        if replacement:
            conn.execute(f"CREATE INDEX idx_session_content_change_rev ON session_content_change{replacement}")
    db._execute_write(break_index)
    assert content().changed_since(reader, before) == (content().FULL, None)
    db._execute_write(lambda c: content().ensure_installed(c))
    dirty, after = content().changed_since(reader, before)
    assert dirty is content().FULL and after.epoch != before.epoch
    plan = reader.execute("EXPLAIN QUERY PLAN SELECT session_id FROM session_content_change WHERE rev>0 LIMIT 3")
    assert any("idx_session_content_change_rev" in row[3] for row in plan)


def test_failed_install_is_not_memoized(tmp_path, monkeypatch):
    import sqlite3
    import hermes_state_schema_memo as memo
    from hermes_state import SessionDB
    memo.clear()
    original = content().ensure_installed
    attempts = []
    def once(conn):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("injected install failure")
        return original(conn)
    monkeypatch.setattr(content(), "ensure_installed", once)
    path = tmp_path / "state.db"
    first = SessionDB(path)
    first.close()
    second = SessionDB(path)
    try:
        assert len(attempts) == 2
        with sqlite3.connect(path) as reader:
            assert content().changed_since(reader, None)[1] is not None
    finally:
        second.close()
