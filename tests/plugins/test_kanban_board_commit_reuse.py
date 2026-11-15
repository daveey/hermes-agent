"""Commit-fenced row reuse must still project live values on every GET."""
from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read as br
from plugins.kanban.dashboard import board_rows
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


def test_write_free_get_skips_materialize_but_projects(board, monkeypatch):
    helpers._seed()
    first = helpers._read(board)
    calls = []
    real = br.materialize
    monkeypatch.setattr(br, "materialize", lambda *a, **k: calls.append(1) or real(*a, **k))
    projected = []
    real_project = br.project
    monkeypatch.setattr(br, "project", lambda *a, **k: projected.append(1) or real_project(*a, **k))
    second = helpers._read(board)
    assert helpers._stable(first) == helpers._stable(second)
    assert not calls, "unchanged observer generation+commit must reuse SQL rows"
    assert projected == [1], "clock/config/replica projection must never be memoized"


def test_bookkeeping_commit_rearms_overlay(board, monkeypatch):
    ids, run_id = helpers._seed()
    helpers._read(board)
    calls = []
    real = br.materialize
    monkeypatch.setattr(br, "materialize", lambda *a, **k: calls.append(1) or real(*a, **k))
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET last_heartbeat_at=12345 WHERE id=?", (ids[0],))
        conn.execute("UPDATE task_runs SET worker_pid=987 WHERE id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    after = helpers._stable(helpers._read(board))
    assert calls == [1]
    assert next(t for c in after['columns'] for t in c['tasks'] if t['id'] == ids[0])['last_heartbeat_at'] == 12345
    assert after == helpers._cold(board)


def test_observer_reset_never_reuses_prior_generation(board, monkeypatch):
    helpers._seed()
    helpers._read(board)
    board_rows.clear()
    calls = []
    real = br.materialize
    monkeypatch.setattr(br, "materialize", lambda *a, **k: calls.append(k.get('prev')) or real(*a, **k))
    helpers._read(board)
    assert calls == [None]


def test_write_free_reuse_keeps_clock_and_profiles_live(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    helpers._seed()
    clock = [10000000000]
    monkeypatch.setattr(br.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "list_profiles_on_disk", lambda: ["first-profile"])
    first = helpers._read(board)
    calls = []
    real = br.materialize
    monkeypatch.setattr(br, "materialize", lambda *a, **k: calls.append(1) or real(*a, **k))
    clock[0] += 60
    monkeypatch.setattr(kb, "list_profiles_on_disk", lambda: ["second-profile"])
    second = helpers._read(board)
    assert not calls
    assert second["now"] == first["now"] + 60
    assert "second-profile" in second["assignees"]
    assert "first-profile" not in second["assignees"]
    old = {t["id"]: t for c in first["columns"] for t in c["tasks"]}
    for col in second["columns"]:
        for task in col["tasks"]:
            assert task["age"]["created_age_seconds"] == old[task["id"]]["age"]["created_age_seconds"] + 60


def test_schema_change_forces_materialize(board, monkeypatch):
    helpers._seed()
    helpers._read(board)
    calls = []
    real = br.materialize
    monkeypatch.setattr(br, "materialize", lambda *a, **k: calls.append(1) or real(*a, **k))
    conn = kbc.connect()
    try:
        conn.execute("CREATE TABLE commit_reuse_schema_test (value)")
        conn.commit()
    finally:
        conn.close()
    result = helpers._read(board)
    assert calls == [1]
    assert helpers._stable(result) == helpers._cold(board)
