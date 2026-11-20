"""Regression probes from the contained-strand exact-head review. [daveey]"""
import sqlite3

import pytest

from hermes_cli import kanban_contained_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wait_info as kwi
from tests.hermes_cli import test_kanban_contained_strand as h

board = h.board


def test_archived_clock_ignores_later_comments(board):
    relay, authority, _ = h._contained(board)
    h._finish(board, authority, "archived", at=None)
    kb._append_event(board, authority, "archived", {})
    board.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='archived'",
                  (h.NOW - 3600, authority))
    before = h._wait(board, relay)
    assert before["agent_owned"] is True
    kb._append_event(board, authority, "commented", {})
    board.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='commented'",
                  (h.NOW - 60, authority))
    after = h._wait(board, relay)
    assert after["agent_owned"] is True
    assert after["waiting_since"] == h.NOW - 3600
    assert after["premise"] == before["premise"]


def test_already_escalated_episode_does_not_rescan_chat_links(board, monkeypatch):
    from hermes_cli import kanban_chat_links
    _relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    assert len(h.sweep.escalate_stranded_contained(board, now=h.NOW)) == 1

    def forbidden(*args):
        pytest.fail("already escalated episode scanned chat links")

    monkeypatch.setattr(kanban_chat_links, "_local_links", forbidden)
    assert h.sweep.escalate_stranded_contained(board, now=h.NOW + 3600) == []


def test_unresolved_episode_is_not_described(board):
    relay, _, _ = h._contained(board)
    # A malformed successor containment makes the reducer unresolved, even though
    # the valid original marker remains among its episodes.
    event = board.execute("SELECT uid, task_id, kind, payload, created_at FROM task_events "
                          "WHERE task_id=? AND kind='contained'", (relay,)).fetchone()
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_events(uid TEXT, task_id TEXT, kind TEXT, payload TEXT, created_at REAL)")
        conn.execute("INSERT INTO task_events VALUES (?, ?, ?, ?, ?)", tuple(event))
        conn.execute("INSERT INTO task_events VALUES ('bad', ?, 'contained', '{\"version\":999}', ?)",
                     (relay, event[4] + 1))
        assert cw._active_episode(conn, relay, event[0]) is None


def test_missing_local_authority_is_read_from_real_replica(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_board_replica_refs as refs
    path = tmp_path / "replica.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE tasks(id TEXT, status TEXT, completed_at REAL);"
                           "CREATE TABLE task_events(task_id TEXT, kind TEXT, created_at REAL);")
        conn.execute("INSERT INTO tasks VALUES ('t_1123456789abcdef', 'done', ?)", (h.NOW - 3600,))
    monkeypatch.setattr(refs, "_replica_paths", lambda: [path])
    assert cw.statuses(board, ["t_1123456789abcdef"]) == {
        "t_1123456789abcdef": ("done", h.NOW - 3600)}


def test_old_producer_run_does_not_allow_wake(board):
    relay, authority, producer = h._contained(board, producer=True)
    h._finish(board, authority)
    at = board.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='contained'",
                       (relay,)).fetchone()[0]
    board.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at) "
                  "VALUES (?, 'done', 'completed', ?, ?)", (producer, at - 20, at - 10))
    assert h._wait(board, relay)["agent_owned"] is False


@pytest.mark.parametrize("status", ["done", "archived"])
def test_terminal_producer_without_new_run_is_stranded(board, status):
    relay, authority, producer = h._contained(board, producer=True)
    h._finish(board, authority)
    h._finish(board, producer, status)
    assert h._wait(board, relay)["agent_owned"] is True


def test_non_triage_replica_does_not_use_contained_facts():
    assert kwi.describe_replica({"status": "ready", "contained": {
        "episode": "episode", "authority": {"id": "authority", "status": "done", "at": h.NOW - 3600}
    }}, now=h.NOW) is None


def test_open_authority_shows_containment_age(board):
    relay, _, _ = h._contained(board)
    at = board.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='contained'",
                       (relay,)).fetchone()[0]
    assert h._wait(board, relay)["waiting_since"] == at
