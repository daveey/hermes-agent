"""Per-part annotation revisions: exact coverage, semantic noise suppression, fail-fresh readers."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_annotation_revision as rev
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import kanban_sync_support as support

ID = ("dev", 1)
CONTAINMENT = (
    "CREATE TABLE containment_canonical_receipts (uid TEXT PRIMARY KEY,board TEXT NOT NULL,task_id TEXT NOT NULL,"
    "kind TEXT NOT NULL,payload TEXT NOT NULL,origin TEXT NOT NULL,evidence TEXT NOT NULL)",
    "CREATE TABLE containment_sync_acceptances (receipt_uid TEXT NOT NULL,sha256 TEXT NOT NULL,evidence TEXT NOT NULL,"
    "reason TEXT NOT NULL,operator TEXT NOT NULL,accepted_at INTEGER NOT NULL,PRIMARY KEY(receipt_uid,sha256,accepted_at))",
    "CREATE TABLE containment_attestations (task_id TEXT NOT NULL, receipt_uid TEXT PRIMARY KEY, episode_uid TEXT NOT NULL,"
    " receipt_sha256 TEXT NOT NULL, episode_sha256 TEXT NOT NULL)",
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    home = support.kanban_home(tmp_path, monkeypatch)
    c = kbc.connect(home / "kanban.db")
    c.row_factory = None
    kb.create_task(c, title="card", assignee="default")
    c.execute("INSERT INTO task_runs(task_id, profile, status, started_at) SELECT id,'default','running',1 FROM tasks")
    c.execute("INSERT INTO kanban_notify_subs(task_id, chat_id, platform, thread_id, last_event_id, created_at) "
              "SELECT id,'chat','tg','',0,1 FROM tasks")
    c.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (board_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,"
              " gateway_id TEXT NOT NULL, hlc_last TEXT NOT NULL, mode TEXT NOT NULL, applying INTEGER NOT NULL DEFAULT 0,"
              " pending_run_uid TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS kanban_sync_remote_parents (parent_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
              " refreshed_at INTEGER NOT NULL)")
    c.execute("INSERT INTO kanban_sync_state(board_id,cursor,gateway_id,hlc_last,mode) VALUES ('b',0,'g','h','m')")
    for ddl in CONTAINMENT:
        c.execute(ddl)
    c.commit()
    rev.ensure_revisions(c)
    yield c
    c.close()
    kbc._INITIALIZED_PATHS.discard(str((home / "kanban.db").resolve()))


def test_batch_reads_schema_and_each_table_once(conn):
    expected = {k.name: rev._read(conn, ID, k) for k in rev.KINDS}
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert rev.read_revisions(conn, ID, rev.KINDS) == expected
    finally:
        conn.set_trace_callback(None)
    assert statements.count("BEGIN") == statements.count("ROLLBACK") == 1
    assert statements.count("PRAGMA schema_version") == 1
    assert statements.count("SELECT name, type, sql FROM sqlite_master") == 1
    columns = [s for s in statements if s.startswith("PRAGMA table_xinfo")]
    assert columns and len(columns) == len(set(columns))
    assert not conn.in_transaction


def test_batch_preserves_caller_transaction_and_refreshes_schema(conn):
    conn.execute("BEGIN")
    before = rev.read_revisions(conn, ID, rev.KINDS)
    assert conn.in_transaction
    conn.rollback()
    conn.execute("ALTER TABLE tasks ADD COLUMN batch_new TEXT")
    conn.commit()
    assert set(rev.read_revisions(conn, ID, rev.KINDS).values()) == {None}
    rev.ensure_revisions(conn)
    after = rev.read_revisions(conn, ID, rev.KINDS)
    assert all(after[k] is not None and after[k] != before[k] for k in before)
    conn.execute("UPDATE tasks SET batch_new='updated'")
    conn.commit()
    newer = rev.read_revisions(conn, ID, rev.KINDS)
    assert all(newer[k] != after[k] for k in after)


def test_batch_unknown_part_does_not_poison_other_parts(conn):
    conn.execute("DROP TRIGGER kanban_annotation_counts_task_events_insert")
    conn.commit()
    values = rev.read_revisions(conn, ID, rev.KINDS)
    assert values['counts'] is None
    assert values['attention'] is not None and values['links'] is not None


def test_batch_concurrent_commit_stays_in_one_snapshot(conn, monkeypatch):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    before = rev.read_revisions(conn, ID, rev.KINDS)
    original = rev._master

    def racing_master(reader):
        result = original(reader)
        other.execute("UPDATE tasks SET title='concurrent batch'")
        other.commit()
        return result

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(rev, '_master', racing_master)
            assert rev.read_revisions(conn, ID, rev.KINDS) == before
        assert not conn.in_transaction
        after = rev.read_revisions(conn, ID, rev.KINDS)
        assert all(after[k] != before[k] for k in before)
    finally:
        other.close()


def test_batch_content_keys_unchanged_change_and_unknown(conn, monkeypatch):
    from pathlib import Path
    from hermes_cli import kanban_activity_revision as activity_revision
    from hermes_cli import kanban_session_activity as activity

    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    before = rev.content_keys(path)
    assert all(v[0] == 'part' for v in before.values())
    assert before == rev.content_keys(path)
    assert before == {k: rev.content_key(path, k) for k in before}
    conn.execute("UPDATE tasks SET title='content changed'")
    conn.commit()
    after = rev.content_keys(path)
    assert all(after[k] != before[k] for k in before)
    original = activity_revision.file_stat
    calls = []

    def replaced(p):
        calls.append(p)
        return original(p) if len(calls) % 2 else (('replacement',), None)

    with monkeypatch.context() as patcher:
        patcher.setattr(activity_revision, 'file_stat', replaced)
        unknown = rev.content_keys(path)
        assert all(v[0] == 'unknown' for v in unknown.values())
        assert all(rev.content_keys(path)[k] != unknown[k] for k in unknown)
    assert rev.content_keys(path) == after
    with activity.cached_ro(path) as reader:
        assert not reader.in_transaction


def test_batch_query_error_rolls_back_and_retries(conn, monkeypatch):
    original = rev._columns

    def broken(*args):
        raise sqlite3.OperationalError('unavailable')

    with monkeypatch.context() as patcher:
        patcher.setattr(rev, '_columns', broken)
        assert set(rev.read_revisions(conn, ID, rev.KINDS).values()) == {None}
        assert not conn.in_transaction
    assert rev._columns is original
    assert all(v is not None for v in rev.read_revisions(conn, ID, rev.KINDS).values())


def both(conn):
    return rev.read_counts_revision(conn, ID), rev.read_attention_revision(conn, ID)


def task(conn):
    return conn.execute("SELECT id FROM tasks").fetchone()[0]


def moved(conn, sql, args=()):
    before = both(conn)
    assert None not in before
    conn.execute(sql, args)
    conn.commit()
    after = both(conn)
    assert None not in after
    return after[0] != before[0], after[1] != before[1]


# (sql, counts moves, attention moves)
CASES = {
    "task title": ("UPDATE tasks SET title = 'x'", True, True),
    "task heartbeat only": ("UPDATE tasks SET last_heartbeat_at = 99", False, False),
    "task row insert": ("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_new','n','ready',1)", True, True),
    "comment row": ("INSERT INTO task_comments(task_id,author,body,created_at) SELECT id,'a','b',1 FROM tasks", False, False),
    "heartbeat event": ("INSERT INTO task_events(task_id,kind,payload,created_at) SELECT id,'heartbeat','{}',1 FROM tasks",
                        True, False),
    "claimed event": ("INSERT INTO task_events(task_id,kind,payload,created_at) SELECT id,'claimed','{}',1 FROM tasks",
                      True, False),
    "run heartbeat": ("UPDATE task_runs SET last_heartbeat_at = 5", True, True),
    "run metadata": ("UPDATE task_runs SET status = 'done'", True, True),
    "notify inflight": ("UPDATE kanban_notify_subs SET inflight_from = 7", True, True),
    "sync cursor": ("UPDATE kanban_sync_state SET cursor = 9, hlc_last = 'z', applying = 1", False, False),
    "sync mode": ("UPDATE kanban_sync_state SET mode = 'other'", True, False),
    "sync gateway": ("UPDATE kanban_sync_state SET gateway_id = 'g2'", True, False),
    "acceptance insert": ("INSERT INTO containment_sync_acceptances VALUES ('u','s','e','r','o',1)", True, False),
    "attestation insert": ("INSERT INTO containment_attestations VALUES ('t','u','e','a','b')", True, False),
    "receipt insert": ("INSERT INTO containment_canonical_receipts VALUES ('u','b','t','k','p','o','e')", True, False),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_semantic_bumps(conn, name):
    sql, counts, attention = CASES[name]
    assert moved(conn, sql) == (counts, attention)


def test_event_delete_and_link_bump_counts_only(conn):
    t = task(conn)
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'x','{}',1)", (t,))
    conn.commit()
    assert moved(conn, "DELETE FROM task_events") == (True, False)
    assert moved(conn, "INSERT INTO task_links(parent_id,child_id) VALUES ('a','b')") == (True, False)


def test_trigger_sql_covers_live_columns_exactly(conn):
    master = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
    cols = [r[1] for r in conn.execute("PRAGMA table_xinfo(tasks)")]
    sql = master["kanban_annotation_counts_tasks_update"]
    assert "last_heartbeat_at" not in sql
    assert all(f'OLD."{c}" IS NOT NEW."{c}"' in sql for c in cols if c != "last_heartbeat_at")
    runs = master["kanban_annotation_attention_task_runs_update"]
    assert all(f'OLD."{r[1]}" IS NOT NEW."{r[1]}"' in runs for r in conn.execute("PRAGMA table_xinfo(task_runs)"))
    assert "last_heartbeat_at" in runs
    sync = master["kanban_annotation_counts_kanban_sync_state_update"]
    assert sync.count("IS NOT NEW") == 3
    assert not any(n.startswith("kanban_annotation_attention_task_events") for n in master)


def test_new_column_refused_until_installer_covers_it(conn):
    assert None not in both(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN brand_new TEXT")
    conn.commit()
    assert both(conn) == (None, None)  # stale triggers do not cover the new column
    rev.ensure_revisions(conn)
    assert None not in both(conn)
    assert moved(conn, "UPDATE tasks SET brand_new = 'v'") == (True, True)


def test_tasks_and_events_unknown_schema_none(conn):
    conn.execute("DROP TRIGGER kanban_annotation_counts_task_events_insert")
    conn.commit()
    assert both(conn)[0] is None and both(conn)[1] is not None  # attention does not read events


@pytest.mark.parametrize("mutate", [
    "DROP TRIGGER kanban_annotation_counts_tasks_delete",
    "DROP TRIGGER kanban_annotation_attention_kanban_notify_subs_update",
    "DROP TRIGGER kanban_annotation_counts_containment_attestations_insert",
    "CREATE TRIGGER kanban_annotation_counts_rogue AFTER INSERT ON tasks BEGIN SELECT 1; END",
    "DROP TABLE kanban_annotation_counts_revision",
    "DELETE FROM kanban_annotation_counts_revision",
    "CREATE TABLE containment_future_input (x TEXT)",
])
def test_unknown_schema_or_triggers_return_none_without_repair(conn, mutate):
    conn.execute(mutate)
    conn.commit()
    schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    master = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    changes = conn.total_changes
    results = both(conn)
    assert None in results
    assert conn.total_changes == changes
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == master
    assert not conn.in_transaction


def test_dropping_optional_table_makes_old_triggers_foreign(conn):
    conn.execute("DROP TABLE containment_attestations")
    conn.commit()
    # its triggers were dropped with it, so coverage is exact again without the table
    assert None not in both(conn)


def test_absent_optional_table_is_certified_by_schema(conn):
    before = rev.read_counts_revision(conn, ID)
    conn.execute("DROP TABLE task_links")
    conn.commit()
    after = rev.read_counts_revision(conn, ID)
    assert after is not None and after != before
    rev.ensure_revisions(conn)
    assert rev.read_counts_revision(conn, ID) == after


def test_missing_required_table_is_none_and_installer_skips(conn):
    conn.execute("DROP TABLE tasks")
    conn.commit()
    assert rev.read_counts_revision(conn, ID) is None
    rev.ensure_revisions(conn)
    assert rev.read_counts_revision(conn, ID) is None
    assert rev.read_attention_revision(conn, ID) is None


def test_view_posing_as_table_or_empty_selection_refused(conn):
    conn.execute("DROP TABLE kanban_sync_state")
    conn.execute("CREATE VIEW kanban_sync_state AS SELECT 1 AS board_id")
    conn.commit()
    assert rev.read_counts_revision(conn, ID) is None
    rev.ensure_revisions(conn)
    assert rev.read_counts_revision(conn, ID) is None


def test_sync_state_missing_selected_column_refused(conn):
    conn.execute("DROP TABLE kanban_sync_state")
    conn.execute("CREATE TABLE kanban_sync_state (board_id TEXT, mode TEXT)")
    conn.commit()
    rev.ensure_revisions(conn)
    assert rev.read_counts_revision(conn, ID) is None


def test_intact_installer_writes_nothing(conn):
    schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    before = both(conn)
    changes = conn.total_changes
    rev.ensure_revisions(conn)
    assert conn.total_changes == changes and both(conn) == before
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema


@pytest.mark.parametrize("lost", ["DELETE FROM kanban_annotation_counts_revision",
                                  "DROP TABLE kanban_annotation_counts_revision"])
def test_repair_after_lost_counter_bumps_and_changes_schema(conn, lost):
    moved(conn, "UPDATE tasks SET title = 'x'")
    attention = rev.read_attention_revision(conn, ID)
    old = rev.read_counts_revision(conn, ID)
    conn.execute(lost)
    conn.commit()
    assert rev.read_counts_revision(conn, ID) is None
    rev.ensure_revisions(conn)
    new = rev.read_counts_revision(conn, ID)
    assert new is not None and new[:2] == old[:2]
    assert new[-1] != old[-1]  # schema generation moved: restarted counter cannot alias a cached key
    assert new != old
    assert rev.read_attention_revision(conn, ID)[2] == attention[2]  # untouched counter stays put


def test_repair_replaces_tampered_trigger_and_drops_rogue(conn):
    conn.execute("DROP TRIGGER kanban_annotation_attention_tasks_update")
    conn.execute("CREATE TRIGGER kanban_annotation_attention_tasks_update AFTER UPDATE ON tasks BEGIN SELECT 1; END")
    conn.execute("CREATE TRIGGER kanban_annotation_counts_rogue AFTER INSERT ON tasks BEGIN SELECT 1; END")
    conn.commit()
    assert rev.read_attention_revision(conn, ID) is None and rev.read_counts_revision(conn, ID) is None
    before = both(conn)
    rev.ensure_revisions(conn)
    after = both(conn)
    assert None not in after
    assert moved(conn, "UPDATE tasks SET title = 'again'") == (True, True)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_annotation_counts_rogue'").fetchone() is None
    assert before == (None, None)


def test_foreign_counter_table_is_never_replaced(conn):
    conn.execute("DROP TABLE kanban_annotation_counts_revision")
    conn.execute("CREATE TABLE kanban_annotation_counts_revision (id INTEGER, revision INTEGER, extra TEXT)")
    conn.execute("INSERT INTO kanban_annotation_counts_revision VALUES (1, 5, 'mine')")
    conn.commit()
    rev.ensure_revisions(conn)
    assert rev.read_counts_revision(conn, ID) is None
    assert conn.execute("SELECT extra FROM kanban_annotation_counts_revision").fetchone() == ("mine",)


def test_reader_uses_one_snapshot(conn, tmp_path):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    conn.execute("BEGIN")
    first = rev.read_counts_revision(conn, ID)
    other.execute("UPDATE tasks SET title = 'concurrent'")
    other.commit()
    assert rev.read_counts_revision(conn, ID) == first  # same snapshot: concurrent commit invisible
    conn.rollback()
    assert rev.read_counts_revision(conn, ID) != first
    other.close()


def test_identity_is_carried(conn):
    assert rev.read_counts_revision(conn, ("x", 2))[:2] == ("x", 2)
    assert rev.read_attention_revision(conn, ("x", 2))[:2] == ("x", 2)


def test_failed_query_returns_none(conn):
    class Boom:
        in_transaction = True

        def execute(self, *_a):
            raise sqlite3.OperationalError("locked")

    assert rev.read_counts_revision(Boom(), ID) is None
