"""Keep the cursor's REPLACE limitation explicit and reproducible. [daveey]"""

import sqlite3
from contextlib import closing

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_task_change as tc


def test_cross_task_replace_limit_is_documented_and_reproduced(tmp_path):
    # A reader cannot inspect the pragma of a different writer connection.
    # This is a documented unsupported write, not a claim that REPLACE is safe.
    assert tc.__doc__ is not None
    assert "INSERT OR REPLACE" in tc.__doc__
    assert "recursive_triggers" in tc.__doc__
    assert "ON CONFLICT DO UPDATE" in tc.__doc__
    path = tmp_path / "board.db"
    with closing(sqlite3.connect(path)) as writer, writer:
        writer.execute("PRAGMA recursive_triggers=OFF")
        writer.execute("CREATE TABLE task_comments (task_id TEXT, body TEXT, uid TEXT UNIQUE)")
        rev.ensure_revision(writer, ("task_comments",))
        writer.execute("INSERT INTO task_comments VALUES ('a', 'original', 'u1')")
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA recursive_triggers=OFF")
        _, cursor = tc.changed_since(reader, None, tables=("task_comments",))
        assert cursor is not None
        writer.execute("INSERT OR REPLACE INTO task_comments VALUES ('b', 'replacement', 'u1')")
        writer.commit()
        assert reader.execute("SELECT task_id FROM task_comments").fetchall() == [("b",)]
        # The hidden delete is not represented, and the trust checks do not return FULL.
        changed, _ = tc.changed_since(reader, cursor, tables=("task_comments",))
        assert changed == {"b"}


def test_upsert_moving_a_unique_key_between_tasks_marks_both(tmp_path):
    path = tmp_path / "board.db"
    with closing(sqlite3.connect(path)) as writer, writer:
        writer.execute("PRAGMA recursive_triggers=OFF")
        writer.execute("CREATE TABLE task_comments (task_id TEXT, body TEXT, uid TEXT UNIQUE)")
        rev.ensure_revision(writer, ("task_comments",))
        writer.execute("INSERT INTO task_comments VALUES ('a', 'original', 'u1')")
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA recursive_triggers=OFF")
        _, cursor = tc.changed_since(reader, None, tables=("task_comments",))
        assert cursor is not None
        writer.execute(
            "INSERT INTO task_comments VALUES ('b', 'replacement', 'u1') "
            "ON CONFLICT(uid) DO UPDATE SET task_id=excluded.task_id, body=excluded.body"
        )
        writer.commit()
        assert reader.execute("SELECT task_id FROM task_comments").fetchall() == [("b",)]
        changed, _ = tc.changed_since(reader, cursor, tables=("task_comments",))
        assert changed == {"a", "b"}
