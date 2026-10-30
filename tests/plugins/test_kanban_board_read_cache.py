"""Kanban dashboard plugin: GET /board reuses its last snapshot.

Desktop polls the board continuously. An unchanged board is read once. A write
is visible once the off-request refresh finishes; a poll that arrives while that
refresh is still running gets the previous snapshot instead of waiting it out.
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


def test_unchanged_board_is_read_once_and_a_write_is_seen(board, monkeypatch):
    from plugins.kanban.dashboard.board_snapshot import MIN_REFRESH_S, join_refresh

    mod, client, reads = board
    monkeypatch.setattr(mod.time, "monotonic", lambda: 1000.0)
    _create("first")
    one = client.get("/api/plugins/kanban/board").json()
    two = client.get("/api/plugins/kanban/board").json()
    assert len(reads) == 1 and _titles(one) == _titles(two) == {"first"}
    assert two["now"] >= one["now"]
    _create("second")
    clock = {"t": 1000.0 + MIN_REFRESH_S + 1}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    three = client.get("/api/plugins/kanban/board").json()
    assert _titles(three) == {"first"}
    join_refresh()
    four = client.get("/api/plugins/kanban/board").json()
    assert len(reads) == 2 and _titles(four) == {"first", "second"}


def test_query_params_are_cached_separately(board):
    _mod, client, reads = board
    client.get("/api/plugins/kanban/board")
    client.get("/api/plugins/kanban/board?include_archived=true")
    assert len(reads) == 2


def test_cache_expires_after_ttl(board, monkeypatch):
    from plugins.kanban.dashboard.board_snapshot import CACHE_TTL_S, join_refresh

    mod, client, reads = board
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    client.get("/api/plugins/kanban/board")
    clock["t"] += CACHE_TTL_S - 1
    client.get("/api/plugins/kanban/board")
    assert len(reads) == 1
    clock["t"] += 2
    client.get("/api/plugins/kanban/board")
    join_refresh()
    assert len(reads) == 2


def test_poll_returns_last_snapshot_while_refresh_is_blocked(board, monkeypatch):
    from plugins.kanban.dashboard.board_snapshot import MIN_REFRESH_S, join_refresh

    mod, client, _reads = board
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    _create("first")
    client.get("/api/plugins/kanban/board")
    clock["t"] += MIN_REFRESH_S + 1
    started = threading.Event()
    release = threading.Event()
    real = mod._get_board_uncached

    def blocked(*args):
        started.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(mod, "_get_board_uncached", blocked)
    _create("second")
    body = client.get("/api/plugins/kanban/board").json()
    assert _titles(body) == {"first"}
    assert started.wait(2)
    release.set()
    join_refresh()
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"first", "second"}


def test_busy_board_is_not_reread_more_often_than_min_refresh(board, monkeypatch):
    """Every kanban commit moves the db signature; a poll must not turn each into a ~1.3s read."""
    from plugins.kanban.dashboard.board_snapshot import MIN_REFRESH_S, join_refresh

    mod, client, reads = board
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    client.get("/api/plugins/kanban/board")
    for i in range(5):
        _create(f"busy{i}")
        clock["t"] += 2
        client.get("/api/plugins/kanban/board")
        join_refresh()
    assert len(reads) == 1
    clock["t"] += MIN_REFRESH_S
    client.get("/api/plugins/kanban/board")
    join_refresh()
    assert len(reads) == 2


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
    seen = []
    real = mod._compute_task_diagnostics
    monkeypatch.setattr(mod, "_compute_task_diagnostics", lambda c, task_ids=None: seen.append(task_ids) or real(c, task_ids))
    body = client.get("/api/plugins/kanban/board?done_limit=2").json()
    done = next(c for c in body["columns"] if c["name"] == "done")
    assert [t["title"] for t in done["tasks"]] == ["done-5", "done-4"]
    assert (done["total"], done["omitted"]) == (6, 4)
    assert len(seen) == 1 and seen[0] is not None
    assert set(seen[0]) == {live, ids[5], ids[4]}


def test_first_read_runs_once_when_prewarm_and_poll_overlap(board, monkeypatch):
    """The boot prewarm owns the cold read; the Desktop's first poll joins it instead of repeating it."""
    mod, client, reads = board
    _create("first")
    started, release = threading.Event(), threading.Event()
    real = mod._get_board_uncached

    def slow(*args):
        started.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(mod, "_get_board_uncached", slow)
    warm = threading.Thread(target=lambda: _prewarm(mod))
    warm.start()
    assert started.wait(5)
    got = {}
    poll = threading.Thread(target=lambda: got.update(client.get("/api/plugins/kanban/board").json()))
    poll.start()
    poll.join(0.5)
    assert poll.is_alive()  # waiting on the warm read, not reading
    release.set()
    warm.join(10)
    poll.join(10)
    assert len(reads) == 1 and _titles(got) == {"first"}


def test_prewarm_makes_the_first_poll_a_snapshot_hit(board):
    mod, client, reads = board
    _create("first")
    _prewarm(mod)
    assert len(reads) == 1
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"first"}
    assert len(reads) == 1


def test_prewarm_thread_is_skipped_under_test_isolation(board, monkeypatch):
    mod, _client, _reads = board
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert start_board_prewarm(mod) is None
