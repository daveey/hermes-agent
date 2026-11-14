"""History ingestion preserves SQLite values without per-field column scans."""
import sqlite3

import pytest

from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_read as br
from hermes_cli import kanban_db_connect as kbc
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


def test_history_rows_are_immutable_keyed_snapshots():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
    conn.execute("INSERT INTO task_events VALUES (1, 't', 'heartbeat', NULL, 42)")
    original = conn.execute("SELECT * FROM task_events").fetchone()
    history = br._rows_by_task(conn, "task_events", ["t", "empty"])
    row = history["t"][0]
    assert not isinstance(row, sqlite3.Row), "normalize column lookup once, not at every diagnostic field read"
    assert history["empty"] == ()
    assert br._raw(history["t"]) == (tuple(original),)
    for field in (*original.keys(), "absent"):
        assert kd._task_field(row, field, "fallback") == kd._task_field(original, field, "fallback")
    with pytest.raises(TypeError):
        row["kind"] = "changed"
    conn.execute("UPDATE task_events SET kind = 'changed'")
    assert row["kind"] == "heartbeat"
    assert br._rows_by_task(conn, "task_events", ["t"])["t"][0]["kind"] == "changed"
    conn.close()


def test_task_only_changes_do_not_reload_event_history(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE tasks (id TEXT, last_heartbeat_at INTEGER);"
                       "CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT);"
                       "CREATE TABLE task_runs (id INTEGER, task_id TEXT, outcome TEXT);"
                       "INSERT INTO tasks VALUES ('t', 1);"
                       "INSERT INTO task_events VALUES (1, 't', 'created');")
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    events = br._rows_by_task(conn, "task_events", ["t"])
    runs = br._rows_by_task(conn, "task_runs", ["t"])
    prev = br._diag_memo(rows, events, runs, br._event_marks(conn, ["t"]))
    monkeypatch.setattr(br, "_wake_states", lambda *_: {})
    conn.execute("UPDATE tasks SET last_heartbeat_at = 2")
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    def unexpected(*args, **kwargs):
        pytest.fail("task bookkeeping must not reload unchanged event/run history")
    inputs, _ = br._incremental_diag_inputs(conn, rows, prev, unexpected, graphs={}, event_changed=frozenset())
    assert inputs[0][0]["last_heartbeat_at"] == 2
    assert inputs[1]["t"] is events["t"]
    conn.close()


@pytest.mark.parametrize("mutation", ["event_update", "event_delete", "event_replace", "run_update", "heartbeat"])
def test_history_mutations_match_cold_projection(board, monkeypatch, mutation):
    from hermes_cli import kanban_db as kb
    ids, run_id = helpers._seed()
    tid = ids[0]
    conn = kbc.connect()
    try:
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'completion_blocked_hallucination', ?, 100)",
                     (tid, '{"phantom_cards":["t_missing"]}'))
        conn.commit()
        helpers._read(board)
        # Observe actual history ingestion rather than the old all-input callback.
        loaded = []
        real = br._rows_by_task
        def spy(c, table, row_ids):
            loaded.append((table, tuple(row_ids)))
            return real(c, table, row_ids)
        monkeypatch.setattr(br, "_rows_by_task", spy)
        event_id = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        mutations = {
            "event_update": ("UPDATE task_events SET kind = 'edited' WHERE id = ?", (event_id,)),
            "event_delete": ("DELETE FROM task_events WHERE id = ?", (event_id,)),
            "event_replace": (
                "INSERT OR REPLACE INTO task_events (id,task_id,kind,payload,created_at) VALUES (?,?,'edited','{}',100)",
                (event_id, tid)),
            "run_update": ("UPDATE task_runs SET outcome = 'failed', ended_at = 150 WHERE id = ?", (run_id,)),
            "heartbeat": ("UPDATE tasks SET last_heartbeat_at = 12345 WHERE id = ?", (tid,)),
        }
        conn.execute(*mutations[mutation])
        conn.commit()
        warm = helpers._stable(helpers._read(board))
        warm_loaded = list(loaded)
        assert warm == helpers._cold(board)
        if mutation == "heartbeat":
            assert not [v for v in warm_loaded if v[0] == "task_events"]
        else:
            assert any(table == "task_events" and tid in touched for table, touched in warm_loaded)
        assert kb.get_task(conn, tid) is not None
    finally:
        conn.close()
