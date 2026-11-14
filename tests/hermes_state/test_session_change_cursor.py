"""[daveey] state.db per-session change cursor (design t_01a1d563324d7e8e §2 P3, review R5 and C8; card D1).

Each case is a session-list input that ``data_version``-free readers would otherwise miss: mid-turn activity
with no message, pin/hide/workspace/archive/title edits, a launch stub becoming real, a new continuation,
lineage receipts, a delete. The trust cases pin the (a)/(b)/(c) rules: an older writer that lacks the
triggers, a broken invariant and a restore all answer FULL.
"""

import os
import shutil
import sqlite3

import pytest

import hermes_state_schema_memo
import hermes_state_session_change as sc
from hermes_state import SessionDB
from hermes_state_common import LOCAL_LINEAGE_PREDICATE_SQL
from hermes_state_session_change import FULL, changed_since


@pytest.fixture
def store(tmp_path):
    hermes_state_schema_memo.clear()
    db = SessionDB(db_path=tmp_path / "state.db")
    reader = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    yield db, reader, tmp_path / "state.db"
    reader.close()
    db.close()


def _cursor(reader):
    ids, cur = changed_since(reader, None)
    assert ids is FULL and cur is not None  # no cursor yet: always a full read
    return cur


def _changed(reader, cur):
    ids, new = changed_since(reader, cur)
    assert ids is not FULL, "expected a trusted incremental answer"
    return set(ids), new


def test_install_is_empty_and_starts_at_full(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    assert changed_since(reader, None)[0] is FULL
    # No backfill: a populated store's install writes no rows (O(1) on the hub's 11.6 GB file).
    other = sqlite3.connect(":memory:")
    other.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, message_count INTEGER)")
    other.executemany("INSERT INTO sessions VALUES (?, 't', 3)", [(f"s{i}",) for i in range(50)])
    assert sc.ensure_installed(other) is True
    assert other.execute("SELECT COUNT(*) FROM session_change").fetchone()[0] == 0
    assert other.execute("SELECT revision FROM session_change_revision").fetchone()[0] == 0
    assert sc.ensure_installed(other) is False  # settled: read-only


def test_mid_turn_activity_without_a_message(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    cur = _cursor(reader)
    db.touch_session_activity("a", ts=4_000_000_000.0)
    ids, cur = _changed(reader, cur)
    assert ids == {"a"}
    # The next mid-turn stamp moves only last_activity_at (same description and provenance).
    db.touch_session_activity("a", ts=4_000_000_060.0)
    ids, _ = _changed(reader, cur)
    assert ids == {"a"}


def test_workspace_root_backfill_alone(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.update_session_cwd("a", "/tmp/repo/sub")
    cur = _cursor(reader)
    db.backfill_repo_roots({"/tmp/repo/sub": "/tmp/repo"})  # moves only git_repo_root: the workspace key
    ids, _ = _changed(reader, cur)
    assert ids == {"a"}


_EDITS = {
    "pinned": lambda db: db.set_session_pinned("a", True),
    "hidden": lambda db: db.set_session_hidden("a", True),
    "workspace": lambda db: db.update_session_cwd("a", "/tmp/elsewhere") or True,
    "archive": lambda db: db.set_session_archived("a", True),
    "title": lambda db: db.set_session_title("a", "Renamed"),
    "source": lambda db: db._execute_write(
        lambda c: c.execute("UPDATE sessions SET source = 'desktop' WHERE id = 'a'")) or True,
}


@pytest.mark.parametrize("edit", sorted(_EDITS))
def test_admission_and_display_edits(store, edit):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    cur = _cursor(reader)
    assert _EDITS[edit](db)
    ids, _ = _changed(reader, cur)
    assert ids == {"a"}


def test_launch_stub_becoming_real_but_not_later_messages(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    cur = _cursor(reader)
    db.append_message("a", "user", "hello")  # 0 -> 1
    ids, cur = _changed(reader, cur)
    assert ids == {"a"}
    db.append_message("a", "assistant", "hi")  # 1 -> 2: a counter tick, messages have their own watermark
    ids, cur = _changed(reader, cur)
    assert ids == set()


def test_counter_only_update_does_not_bump(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.append_message("a", "user", "hello")
    cur = _cursor(reader)
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET input_tokens = input_tokens + 5, api_call_count = api_call_count + 1, "
        "tool_call_count = tool_call_count + 1, transcript_revision = transcript_revision + 1 WHERE id = 'a'"))
    assert _changed(reader, cur)[0] == set()


def test_new_continuation_moves_parent_and_child(store):
    db, reader, _ = store
    db.create_session("root", "cli")
    db.append_message("root", "user", "hello")
    cur = _cursor(reader)
    db.end_session("root", "compression")
    db.create_session("cont", "cli", parent_session_id="root")
    ids, _ = _changed(reader, cur)
    assert ids == {"root", "cont"}


def test_lineage_marker_in_model_config(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    cur = _cursor(reader)
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET model_config = json_set(COALESCE(model_config, '{}'), '$._branched_from', 'a') "
        "WHERE id = 'b'"))
    assert _changed(reader, cur)[0] == {"b"}


def test_local_lineage_receipt_reads_full(store):
    """A receipt that supersedes segments changes which rows a listing shows; it is rare (a local reset),
    so it moves the lineage counter and the next read is FULL, after which the cursor is precise again."""
    db, reader, _ = store
    for sid in ("own", "seg2", "seg3"):
        db.create_session(sid, "cli")
    cur = _cursor(reader)
    key = "gateway.local_policy.v1:own"
    db._execute_write(lambda c: c.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)",
        (key, '{"session_id": "own", "lineage": ["own", "seg2"]}')))
    ids, cur = changed_since(reader, cur)
    assert ids is FULL and cur is not None
    assert changed_since(reader, cur)[0] == set()
    db._execute_write(lambda c: c.execute(
        "UPDATE state_meta SET value = ? WHERE key = ?",
        ('{"session_id": "own", "lineage": ["own", "seg2", "seg3"]}', key)))
    ids, cur = changed_since(reader, cur)
    assert ids is FULL
    db._execute_write(lambda c: c.execute("DELETE FROM state_meta WHERE key = ?", (key,)))
    ids, cur = changed_since(reader, cur)
    assert ids is FULL
    # Unrelated keys, single-segment and malformed receipts never move it, and never fail the write.
    db._execute_write(lambda c: c.execute("INSERT INTO state_meta (key, value) VALUES ('other', 'x')"))
    db._execute_write(lambda c: c.execute(
        "INSERT INTO state_meta (key, value) VALUES ('gateway.local_policy.v1:one', "
        "'{\"session_id\": \"one\", \"lineage\": [\"one\"]}')"))
    db._execute_write(lambda c: c.execute(
        "INSERT INTO state_meta (key, value) VALUES ('gateway.local_policy.v1:bad', 'not json')"))
    assert changed_since(reader, cur)[0] == set()
    plan = " ".join(str(r[-1]) for r in reader.execute(
        "EXPLAIN QUERY PLAN SELECT key, value FROM state_meta WHERE " + LOCAL_LINEAGE_PREDICATE_SQL + " ORDER BY key"))
    assert "idx_state_meta_local_lineage" in plan  # a poll reads the few receipts, never all of state_meta


def test_rekeyed_row_moves_both_ids(store):
    db, reader, _ = store
    db.create_session("old", "cli")
    cur = _cursor(reader)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET id = 'new' WHERE id = 'old'"))
    assert _changed(reader, cur)[0] == {"old", "new"}


def test_delete_and_insert_or_replace(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    cur = _cursor(reader)
    db.delete_session("a")
    ids, cur = _changed(reader, cur)
    assert ids == {"a"}
    assert reader.execute("SELECT 1 FROM sessions WHERE id = 'a'").fetchone() is None  # caller: missing = deleted
    # INSERT OR REPLACE fires no DELETE trigger without recursive_triggers; the INSERT upsert covers it.
    db._execute_write(lambda c: c.execute(
        "INSERT OR REPLACE INTO sessions (id, source, started_at) VALUES ('b', 'cli', 1.0)"))
    assert _changed(reader, cur)[0] == {"b"}


def test_older_writer_without_triggers_reads_full(store):
    db, reader, path = store
    db.create_session("a", "cli")
    cur = _cursor(reader)
    # A store rebuilt or copied by a build that predates the cursor carries no triggers.
    db._execute_write(lambda c: sc.drop_triggers(c))
    db.set_session_title("a", "Unseen by the cursor")
    ids, again = changed_since(reader, cur)
    assert ids is FULL and again is None
    # A current writer's init reinstalls them and rotates the epoch: the pre-gap cursor stays untrusted.
    db.close()
    hermes_state_schema_memo.clear()
    db2 = SessionDB(db_path=path)
    try:
        assert changed_since(reader, cur)[0] is FULL
        fresh = _cursor(reader)
        db2.set_session_title("a", "Seen")
        assert _changed(reader, fresh)[0] == {"a"}
    finally:
        db2.close()


def test_reinstall_without_schema_change_invalidates_prior_epoch(store):
    db, reader, _ = store
    cur = _cursor(reader)
    assert cur.revision == 0
    # A missing singleton needs repair, but the existing tables/triggers need no DDL.
    # Unlike dropped-trigger repair, no schema-cookie change can mask epoch reuse.
    db._execute_write(lambda c: c.execute("DELETE FROM session_change_revision"))
    assert changed_since(reader, cur) == (FULL, None)
    assert sc.ensure_installed(reader) is True
    ids, fresh = changed_since(reader, cur)
    assert fresh is not None
    assert (fresh.dev, fresh.ino, fresh.shm, fresh.schema_cookie,
            fresh.schema_level, fresh.lineage, fresh.revision) == (
                cur.dev, cur.ino, cur.shm, cur.schema_cookie,
                cur.schema_level, cur.lineage, cur.revision)
    assert ids is FULL  # Only the install epoch distinguishes these valid snapshots.
    assert fresh.epoch != cur.epoch
    # Ordinary stable init must neither write nor invalidate the repaired cursor.
    changes = reader.total_changes
    assert sc.ensure_installed(reader) is False
    assert reader.total_changes == changes
    assert changed_since(reader, fresh) == (frozenset(), fresh)
    db.create_session("after-repair", "cli")
    assert _changed(reader, fresh)[0] == {"after-repair"}


def test_stale_trigger_set_reads_full(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    cur = _cursor(reader)
    # A newer build added a column: until this build's init regenerates the triggers, the column is unwatched.
    db._execute_write(lambda c: c.execute("ALTER TABLE sessions ADD COLUMN future_flag INTEGER"))
    assert changed_since(reader, cur) == (FULL, None)


def test_bump_without_upsert_breaks_invariant(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    cur = _cursor(reader)
    db._execute_write(lambda c: c.execute("UPDATE session_change_revision SET revision = revision + 1"))
    assert changed_since(reader, cur) == (FULL, None)  # (a): MAX(rev) != revision


def test_in_place_restore_reads_full(store, tmp_path):
    db, reader, path = store
    db.create_session("a", "cli")
    backup = sqlite3.connect(tmp_path / "backup.db")
    src = sqlite3.connect(path)
    src.backup(backup)
    src.close()
    db.create_session("b", "cli")
    db.set_session_title("a", "After the backup")
    cur = _cursor(reader)
    # Backup-API restore over the live file: same inode, regressed revision (b); the backup also moves the
    # destination's schema cookie (c).
    dst = sqlite3.connect(path)
    backup.backup(dst)
    dst.close()
    backup.close()
    assert changed_since(reader, cur)[0] is FULL


def test_regressed_revision_with_same_schema_reads_full(store):
    db, reader, _ = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    before = dict(reader.execute("SELECT session_id, rev FROM session_change").fetchall())
    mid = reader.execute("SELECT revision FROM session_change_revision").fetchone()[0]
    db.set_session_title("a", "later")
    cur = _cursor(reader)
    assert cur.revision > mid
    # The rows as they were at ``mid``, under an unchanged schema cookie and epoch (a row-level restore):
    # (a) still holds, only (b) catches it.
    def roll_back(conn):
        conn.executemany("UPDATE session_change SET rev = ? WHERE session_id = ?",
                         [(rev, sid) for sid, rev in before.items()])
        conn.execute("UPDATE session_change_revision SET revision = ?", (mid,))
    db._execute_write(roll_back)
    assert changed_since(reader, cur)[0] is FULL


def test_file_replacing_restore_reads_full(store, tmp_path):
    db, reader, path = store
    db.create_session("a", "cli")
    cur = _cursor(reader)
    db.close()
    shutil.copy(path, tmp_path / "restored.db")
    os.replace(tmp_path / "restored.db", path)  # new inode (c)
    fresh_reader = sqlite3.connect(path, isolation_level=None)
    try:
        assert changed_since(fresh_reader, cur)[0] is FULL
    finally:
        fresh_reader.close()


def test_limit_answers_full_past_it(store):
    db, reader, _ = store
    cur = _cursor(reader)
    for sid in ("a", "b", "c"):
        db.create_session(sid, "cli")
    assert changed_since(reader, cur, limit=2)[0] is FULL
    assert set(changed_since(reader, cur, limit=3)[0]) == {"a", "b", "c"}
