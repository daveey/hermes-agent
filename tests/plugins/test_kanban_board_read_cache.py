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


def test_unchanged_board_is_read_once_and_a_write_is_seen(board):
    from plugins.kanban.dashboard.board_snapshot import join_refresh

    _mod, client, reads = board
    _create("first")
    one = client.get("/api/plugins/kanban/board").json()
    two = client.get("/api/plugins/kanban/board").json()
    assert len(reads) == 1 and _titles(one) == _titles(two) == {"first"}
    assert two["now"] >= one["now"]
    _create("second")
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
    from plugins.kanban.dashboard.board_snapshot import join_refresh

    mod, client, _reads = board
    _create("first")
    client.get("/api/plugins/kanban/board")
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
