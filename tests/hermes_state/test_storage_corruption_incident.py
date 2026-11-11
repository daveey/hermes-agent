"""Latch reporting uses an independent Kanban store and reuses only live incidents."""
from contextlib import closing

import hermes_state_corruption_confirm as cc
import hermes_state_corruption_incident as incident
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_db as kb
from hermes_state_health import mark_storage_corrupt, reset_storage_state


def test_latch_reports_only_first_error(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(incident, "schedule_incident", lambda *args: seen.append(args))
    path = tmp_path / "state.db"
    reset_storage_state(path)
    mark_storage_corrupt(path, "first corrupt line")
    mark_storage_corrupt(path, "later corrupt line")
    assert seen == [(path, "first corrupt line")]
    reset_storage_state(path)


def test_incident_card_contains_first_line_and_table_results(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(cc, "confirm_corruption", lambda *args: cc.ConfirmResult(
        "damaged", "messages,sessions", 6.0, "bad page",
        (("messages", "bad page 42"), ("sessions", "ok"))))
    first = incident.report_incident(tmp_path, "SQLITE_CORRUPT original")
    assert first
    assert incident.report_incident(tmp_path, "repeated") == first
    with closing(connect(tmp_path / "kanban.db")) as conn:
        task = kb.get_task(conn, first)
        assert task is not None and task.body
        assert "SQLITE_CORRUPT original" in task.body
        comments = kb.list_comments(conn, first)
        assert len(comments) == 2
        assert "messages: bad page 42" in comments[0].body
        assert "sessions: ok" in comments[0].body
        assert "Another storage owner latched: repeated" in comments[1].body
        # Exercise a resolved predecessor: a new latch must get a new repair owner.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
        conn.commit()
    second = incident.report_incident(tmp_path, "new incident")
    assert second != first


def test_scratch_database_never_schedules_live_card(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scratch DB must not spawn a reporter")
    monkeypatch.setattr(incident.threading, "Thread", forbidden)
    incident.schedule_incident(tmp_path / "scratch.db", "fixture")


def test_real_isolated_child_returns_per_table_results(tmp_path):
    import sqlite3

    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
        conn.commit()
    result = cc.confirm_corruption(path, frozenset(), None)
    assert result.clean
    assert ("messages", "ok") in result.table_results
    assert ("sqlite_schema", "ok") in result.table_results


def test_isolated_child_exits_if_parent_identity_is_gone(tmp_path):
    import os
    import sqlite3
    import subprocess
    import pytest

    if os.name != "posix":
        pytest.skip("POSIX parent lifetime guard")
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER)")
        conn.commit()
        conn.execute("BEGIN EXCLUSIVE")
        argv = cc._child_argv(path.as_uri() + "?mode=ro", "null", 30000)
        argv[-1] = "-1"  # a parent PID which cannot be this process's real parent
        proc = subprocess.run(argv, capture_output=True, timeout=5, check=False, start_new_session=True)
        assert proc.returncode == 1
        assert proc.stdout == b""


def test_per_table_results_survive_real_damage(tmp_path):
    import sqlite3

    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE broken(id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("CREATE TABLE healthy(id INTEGER PRIMARY KEY)")
        conn.commit()
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name='broken'").fetchone()[0]
    with path.open("r+b") as stream:
        stream.seek(size * (root - 1))
        stream.write(b"\xff" * size)
    result = cc.confirm_corruption(path, frozenset(), None)
    assert result.outcome == "damaged"
    assert ("healthy", "ok") in result.table_results
    assert any(table == "broken" and value != "ok" for table, value in result.table_results)
