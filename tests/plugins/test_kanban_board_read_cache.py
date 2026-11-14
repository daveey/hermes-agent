"""GET /board projects exact next-GET state over retained, incremental SQL rows.

Desktop polls must see committed writes, heartbeats and clock changes immediately,
without repeating a full board materialization on every poll.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_cache_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    mod = _load_plugin()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    reads = []
    real = mod._get_board_uncached
    monkeypatch.setattr(mod, "_get_board_uncached", lambda *a: reads.append(a) or real(*a))
    return mod, TestClient(app), reads


def _titles(body: dict) -> set[str]:
    return {t["title"] for col in body["columns"] for t in col["tasks"]}


def _create(title: str) -> None:
    conn = kbc.connect()
    try:
        kb.create_task(conn, title=title, assignee="worker")
    finally:
        conn.close()


def _full_reads(monkeypatch):
    reads = []
    real = kb.list_tasks
    monkeypatch.setattr(kb, "list_tasks", lambda *a, **k: reads.append(1) or real(*a, **k))
    return reads


def test_unchanged_board_materializes_once_and_write_is_seen_on_next_get(board, monkeypatch):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 1000.0)
    _create("first")
    one = client.get("/api/plugins/kanban/board").json()
    two = client.get("/api/plugins/kanban/board").json()
    assert _titles(one) == _titles(two) == {"first"}
    assert two["now"] >= one["now"]
    _create("second")
    three = client.get("/api/plugins/kanban/board").json()
    assert _titles(three) == {"first", "second"}
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"first", "second"}
    assert len(sql_reads) == 1


def test_query_params_are_cached_separately(board, monkeypatch):
    _mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="archived", assignee="worker")
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    _create("live")
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"live"}
    assert _titles(client.get("/api/plugins/kanban/board?include_archived=true").json()) == {"live", "archived"}
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"live"}
    assert len(sql_reads) == 2


def test_clock_changes_are_projected_on_next_get_without_ttl_wait(board, monkeypatch):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 1000.0)
    clock = {"t": float(int(mod.time.time()))}
    monkeypatch.setattr(mod.time, "time", lambda: clock["t"])
    _create("first")
    one = client.get("/api/plugins/kanban/board").json()
    clock["t"] += 1
    two = client.get("/api/plugins/kanban/board").json()
    first = next(t for c in one["columns"] for t in c["tasks"])
    later = next(t for c in two["columns"] for t in c["tasks"])
    assert later["age"]["created_age_seconds"] == first["age"]["created_age_seconds"] + 1
    assert two["now"] > one["now"]
    assert len(sql_reads) == 1


def test_poll_waits_for_exact_read_instead_of_serving_stale_snapshot(board, monkeypatch):
    mod, client, _reads = board
    _create("first")
    client.get("/api/plugins/kanban/board")
    started, release = threading.Event(), threading.Event()
    real = mod._get_board_uncached

    def blocked(*args):
        started.set()
        assert release.wait(10), "test did not release exact board read"
        return real(*args)

    monkeypatch.setattr(mod, "_get_board_uncached", blocked)
    _create("second")
    got = {}
    poll = threading.Thread(target=lambda: got.update(client.get("/api/plugins/kanban/board").json()))
    poll.start()
    try:
        assert started.wait(5)
        assert poll.is_alive()
        assert got == {}
    finally:
        release.set()
        poll.join(10)
    assert not poll.is_alive()
    assert _titles(got) == {"first", "second"}


def test_busy_board_sees_every_write_without_full_rematerialization(board, monkeypatch):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 1000.0)
    _create("busy0")
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"busy0"}
    for i in range(1, 6):
        _create(f"busy{i}")
        assert _titles(client.get("/api/plugins/kanban/board").json()) == {f"busy{j}" for j in range(i + 1)}
    assert len(sql_reads) == 1


def test_heartbeat_is_visible_on_next_get_without_refresh_wait(board, monkeypatch):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 1000.0)
    _create("heartbeat")
    one = client.get("/api/plugins/kanban/board").json()
    card = next(t for c in one["columns"] for t in c["tasks"])
    assert card["last_heartbeat_at"] is None
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET last_heartbeat_at = 1234567 WHERE id = ?", (card["id"],))
        conn.commit()
    finally:
        conn.close()
    two = client.get("/api/plugins/kanban/board").json()
    later = next(t for c in two["columns"] for t in c["tasks"])
    assert later["last_heartbeat_at"] == 1234567
    assert len(sql_reads) == 1


from plugins.kanban.dashboard.board_prewarm import prewarm_boards as _prewarm, start_board_prewarm


def _complete(conn, task_id: str, at: int) -> None:
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (at, task_id))
    conn.commit()


def test_cold_read_computes_diagnostics_only_for_cards_in_the_payload(board, monkeypatch):
    """Done cards past done_limit are dropped from the payload; the cold read must not run the
    diagnostics rules over them (~1.2 of 1.5 s on the hub's 1.2k-card board)."""
    mod, client, _reads = board
    ids = []
    conn = kbc.connect()
    try:
        for i in range(6):
            ids.append(kb.create_task(conn, title=f"done-{i}", assignee="w"))
            _complete(conn, ids[-1], 100 + i)
        live = kb.create_task(conn, title="live", assignee="w")
    finally:
        conn.close()
    seen, ruled = [], []
    # The board reads the diagnostics rows (SQL) and runs the rules in separate steps, so
    # retained rows can be re-ruled on each refresh; both must be limited to payload cards.
    real = mod._diagnostic_inputs
    monkeypatch.setattr(mod, "_diagnostic_inputs", lambda c, task_ids=None: seen.append(task_ids) or real(c, task_ids))
    real_rules = mod.kd.compute_task_diagnostics
    monkeypatch.setattr(mod.kd, "compute_task_diagnostics", lambda r, *a, **k: ruled.append(r["id"]) or real_rules(r, *a, **k))
    body = client.get("/api/plugins/kanban/board?done_limit=2").json()
    done = next(c for c in body["columns"] if c["name"] == "done")
    assert [t["title"] for t in done["tasks"]] == ["done-5", "done-4"]
    assert (done["total"], done["omitted"]) == (6, 4)
    assert len(seen) == 1 and seen[0] is not None
    assert set(seen[0]) == {live, ids[5], ids[4]}
    assert sorted(ruled) == sorted([live, ids[5], ids[4]])


def test_first_read_runs_once_when_prewarm_and_poll_overlap(board, monkeypatch):
    """The boot prewarm owns the cold read; the Desktop's first poll joins it instead of repeating it."""
    mod, client, _reads = board
    _create("first")
    started, release = threading.Event(), threading.Event()
    sql_reads = []
    real = kb.list_tasks

    def slow(*args, **kwargs):
        sql_reads.append(1)
        started.set()
        assert release.wait(10), "test did not release prewarm materialization"
        return real(*args, **kwargs)

    monkeypatch.setattr(kb, "list_tasks", slow)
    warm = threading.Thread(target=lambda: _prewarm(mod))
    got = {}
    poll = threading.Thread(target=lambda: got.update(client.get("/api/plugins/kanban/board").json()))
    warm.start()
    try:
        assert started.wait(5)
        poll.start()
        poll.join(0.5)
        assert poll.is_alive()  # waiting on the warm rows, not materializing again
    finally:
        release.set()
        warm.join(10)
        if poll.ident is not None:
            poll.join(10)
    assert not warm.is_alive() and not poll.is_alive()
    assert len(sql_reads) == 1 and _titles(got) == {"first"}


def test_prewarm_makes_the_first_poll_reuse_materialized_rows(board, monkeypatch):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    _create("first")
    _prewarm(mod)
    assert len(sql_reads) == 1
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"first"}
    assert len(sql_reads) == 1
    _create("second")
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"first", "second"}
    assert len(sql_reads) == 1


def test_prewarm_thread_is_skipped_under_test_isolation(board, monkeypatch):
    mod, _client, _reads = board
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert start_board_prewarm(mod) is None
