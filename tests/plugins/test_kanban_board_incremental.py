"""Kanban dashboard plugin: a board refresh after a commit re-reads every row but re-parses only
what changed. A heartbeat touches one task (its row, run and one event); the refresh must not
re-parse the other cards or re-read their events (~1.2 s of CPU per refresh on the hub's board),
and its payload must equal a cold read's."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_incremental_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    from plugins.kanban.dashboard import board_rows

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    board_rows.clear()
    yield _load_plugin()
    board_rows.clear()


def _read(mod, done_limit=3):
    return mod._get_board_uncached(None, False, None, None, None, done_limit)


def _stable(body: dict) -> dict:
    """The payload without the clock-derived fields."""
    body = dict(body)
    body.pop("now", None)
    cols = []
    for col in body["columns"]:
        tasks = [{k: v for k, v in t.items() if k != "age"} for t in col["tasks"]]
        cols.append({**col, "tasks": tasks})
    body["columns"] = cols
    return body


def _cold(mod, done_limit=3):
    from plugins.kanban.dashboard import board_rows

    board_rows.clear()
    return _stable(_read(mod, done_limit))


def _seed():
    conn = kbc.connect()
    try:
        ids = [kb.create_task(conn, title=f"card-{i}", assignee="worker") for i in range(6)]
        for i, tid in enumerate(ids[:3]):
            kb.add_comment(conn, tid, "me", f"note {i}")
        kb.claim_task(conn, ids[0])
        run_id = kb.latest_run(conn, ids[0]).id
        for i, tid in enumerate(ids[3:5]):
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (100 + i, tid))
        conn.commit()
        return ids, run_id
    finally:
        conn.close()


def test_heartbeat_refresh_reparses_and_rereads_only_the_beating_task(board, monkeypatch):
    mod = board
    ids, run_id = _seed()
    _read(mod)
    parsed, reread = [], []
    real_from_row = kb.Task.from_row.__func__
    monkeypatch.setattr(kb.Task, "from_row", classmethod(lambda cls, r: parsed.append(r["id"]) or real_from_row(cls, r)))
    real_inputs = mod._diagnostic_inputs
    monkeypatch.setattr(mod, "_diagnostic_inputs", lambda c, task_ids=None: reread.append(task_ids) or real_inputs(c, task_ids))
    conn = kbc.connect()
    try:
        for _ in range(3):  # bookkeeping churn: the worker's heartbeat, then a poll
            assert kbd.heartbeat_worker(conn, ids[0], expected_run_id=run_id)
            body = _stable(_read(mod))
    finally:
        conn.close()
    # The other cards keep their parsed rows (the beating one re-parses when its clock moved).
    assert set(parsed) <= {ids[0]}, parsed
    assert reread and all(task_ids == [ids[0]] for task_ids in reread), reread
    assert body == _cold(mod)


def test_unchanged_rows_after_an_unrelated_commit_are_not_reparsed(board, monkeypatch):
    mod = board
    _seed()
    _read(mod)
    parsed, reread = [], []
    real_from_row = kb.Task.from_row.__func__
    monkeypatch.setattr(kb.Task, "from_row", classmethod(lambda cls, r: parsed.append(r["id"]) or real_from_row(cls, r)))
    real_inputs = mod._diagnostic_inputs
    monkeypatch.setattr(mod, "_diagnostic_inputs", lambda c, task_ids=None: reread.append(task_ids) or real_inputs(c, task_ids))
    conn = kbc.connect()
    try:  # a commit the board does not read moves data_version
        conn.execute("CREATE TABLE IF NOT EXISTS test_scratch (x)")
        conn.execute("INSERT INTO test_scratch VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    body = _stable(_read(mod))
    assert parsed == [] and reread == []
    assert body == _cold(mod)


def test_incremental_refresh_matches_a_cold_read_after_mixed_writes(board):
    mod = board
    ids, run_id = _seed()
    before = _read(mod)
    conn = kbc.connect()
    try:
        kb.add_comment(conn, ids[1], "me", "another")                        # comment count
        kb.block_task(conn, ids[2], reason="waiting")                        # status + event
        kb.complete_task(conn, ids[0], summary="done", expected_run_id=run_id,
                         fire_lifecycle_hook=False)                          # run end + done column
        new = kb.create_task(conn, title="late", assignee="worker")          # new card
        kb.link_tasks(conn, ids[1], new)                                     # links + progress
        conn.execute("DELETE FROM task_events WHERE id = (SELECT MAX(id) FROM task_events WHERE task_id = ?)",
                     (ids[2],))                                              # withdrawn event
        conn.commit()
    finally:
        conn.close()
    after = _stable(_read(mod))
    assert after != _stable(before)
    assert after == _cold(mod)
    done = next(c for c in after["columns"] if c["name"] == "done")
    assert done["total"] == 3 and len(done["tasks"]) == 3


def test_done_cards_past_the_limit_are_never_parsed(board, monkeypatch):
    mod = board
    conn = kbc.connect()
    try:
        ids = [kb.create_task(conn, title=f"d{i}", assignee="w") for i in range(8)]
        for i, tid in enumerate(ids):
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (100 + i, tid))
        conn.commit()
    finally:
        conn.close()
    parsed = []
    real_from_row = kb.Task.from_row.__func__
    monkeypatch.setattr(kb.Task, "from_row", classmethod(lambda cls, r: parsed.append(r["id"]) or real_from_row(cls, r)))
    body = _read(mod, done_limit=2)
    done = next(c for c in body["columns"] if c["name"] == "done")
    assert [t["title"] for t in done["tasks"]] == ["d7", "d6"]
    assert (done["total"], done["omitted"]) == (8, 6)
    assert sorted(parsed) == sorted([ids[7], ids[6]])
