"""Regression: dashboard requests must not re-run kanban board init (integrity_check) per call.

``plugin_api._conn`` used to call ``init_db`` on every request. ``init_db`` drops the board's
``_INITIALIZED_PATHS`` entry, so each GET /board re-ran ``PRAGMA integrity_check`` over the whole
board file (3-25 s on a 140 MB kanban.db) and starved the Desktop backend's REST API.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc._INITIALIZED_PATHS.clear()
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_conn_no_reinit_test", path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    yield mod
    kbc._INITIALIZED_PATHS.clear()


def test_repeated_requests_probe_integrity_once(plugin, monkeypatch):
    probes = []
    real = kbc._run_integrity_check
    monkeypatch.setattr(kbc, "_run_integrity_check", lambda conn: probes.append(1) or real(conn))
    # Seed a non-empty board file so the integrity guard has something to probe.
    kb.init_db()
    probes.clear()
    for _ in range(5):
        with plugin._board_conn(None) as (_, conn):
            conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
    assert probes == [], f"dashboard connect re-ran integrity_check {len(probes)}x"


def test_fresh_install_still_self_initialises(plugin):
    """connect() alone creates the schema, so POST /tasks first on a fresh home still works."""
    with plugin._board_conn(None) as (_, conn):
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
