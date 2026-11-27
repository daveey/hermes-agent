"""Completion-receipt readers probe board columns in one handed-out row. [daveey]

``read_candidates``/``origin_of`` run once per owner card in the remote-links projection
(``observe_owner_task`` -> ``completion_verdict``), and ``PRAGMA table_info(task_events)`` handed one
row per column to Python: each a GIL handoff under the serve's convoy. Live py-spy on the hub serve put
117 of 986 links-projection samples on that probe (t_01a20286f9f3f262).
"""
import sqlite3

from hermes_cli import kanban_completion_refs as refs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _counting(conn):
    handed = {"rows": 0}
    inner = conn.row_factory

    def factory(cursor, row):
        handed["rows"] += 1
        return inner(cursor, row) if inner is not None else row

    conn.row_factory = factory
    return handed


def test_column_probes_hand_out_one_row(tmp_path):
    with kbc.connect(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title="card", assignee="dev")
        expected = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
        handed = _counting(conn)
        assert refs._columns(conn) == expected
        assert handed["rows"] == 1
        handed["rows"] = 0
        assert refs.read_candidates(conn, [tid]) == {tid: []}
        assert handed["rows"] == 1  # the column probe; no receipt rows
        assert refs.leased_elsewhere(conn, {tid: 1}) == {}


def test_missing_columns_still_read_as_null(tmp_path):
    conn = sqlite3.connect(tmp_path / "bare.db")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('t1', 'pr_acceptance', '{}')")
    rows = refs.read_candidates(conn, ["t1"])["t1"]
    assert rows == [{"id": 1, "uid": None, "task_id": "t1", "kind": "pr_acceptance", "run_id": None,
                     "origin_gateway": None, "payload": "{}"}]
    assert refs.origin_of(conn, 1) is None
    assert refs.leased_elsewhere(conn, {"t1": 1}) == {}  # no uid column on task_runs
