"""Unknown process ancestry must not grant board mutation rights."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent import delegation_context as dc
from hermes_cli import kanban_db as kb


def test_minimal_interpreter_cannot_drop_inherited_fence(tmp_path):
    production = tmp_path / "production"
    pinned = production / "kanban.db"
    env = dict(os.environ)
    env.update({dc.DELEGATED_CHILD_ENV_MARKER: str(production),
                dc.DELEGATED_CHILD_OWNER_ENV: dc._owner_token(),
                "HERMES_KANBAN_DB": str(pinned), "HERMES_HOME": str(tmp_path / "scratch")})
    # -S excludes site-packages: reproduce a scratch interpreter with no psutil,
    # rather than relying only on a patched dependency in the pytest interpreter.
    probe = """
import os
from agent import delegation_context as dc
try:
    import psutil
except ImportError:
    pass
else:
    raise AssertionError('probe must run without psutil')
assert dc.is_delegated_child_process_context()
assert dc.kanban_path_is_fenced(os.environ['HERMES_KANBAN_DB'])
assert not dc.kanban_path_is_fenced(os.path.join(os.environ['HERMES_HOME'], 'kanban.db'))
print('minimal interpreter fence retained')
"""
    result = subprocess.run([sys.executable, "-S", "-c", probe], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "minimal interpreter fence retained" in result.stdout
    assert not pinned.exists()


@pytest.mark.parametrize("failure", ["missing_psutil", "process_lookup", "fingerprint", "depth_limit"])
def test_unknown_lineage_keeps_fence_and_allows_isolated_db(tmp_path, monkeypatch, failure):
    import psutil

    production = tmp_path / "production"
    production.mkdir()
    pinned = production / "kanban.db"
    scratch = tmp_path / "scratch.db"
    owner = os.getppid()
    fingerprint = dc._start_fingerprint(owner)
    assert fingerprint is not None
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, str(production))
    monkeypatch.setenv(dc.DELEGATED_CHILD_OWNER_ENV, f"{owner}:{fingerprint}")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(production))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    dc._live_cache.clear()
    if failure == "missing_psutil":
        monkeypatch.setitem(sys.modules, "psutil", None)
    elif failure == "process_lookup":
        def denied(pid):
            raise psutil.AccessDenied(pid)
        monkeypatch.setattr(psutil, "Process", denied)
    elif failure == "fingerprint":
        monkeypatch.setattr(dc, "_start_fingerprint", lambda pid: None)
    else:
        monkeypatch.setattr(dc, "_parent_pid", lambda pid: pid + 1)
    try:
        assert dc.is_delegated_child_process_context()
        assert os.environ["HERMES_KANBAN_DB"] == str(pinned)
        assert dc.kanban_path_is_fenced(pinned)
        with pytest.raises(PermissionError):
            kb._assert_not_delegated_child_mutation(pinned)
        # Fenced connection initialization is read-only and cannot create a DB.
        with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
            kb.init_db(pinned)
        assert not pinned.exists()
        assert not dc.kanban_path_is_fenced(scratch)
        # Real schema creation and transaction on an isolated DB still work.
        from hermes_cli.kanban_db_connect import connect
        conn = connect(scratch)
        try:
            with kb.write_txn(conn):
                conn.execute("CREATE TABLE isolation_probe (value TEXT)")
                conn.execute("INSERT INTO isolation_probe VALUES ('allowed')")
            assert conn.execute("SELECT value FROM isolation_probe").fetchone()[0] == "allowed"
        finally:
            conn.close()
    finally:
        dc._live_cache.clear()


def test_verified_detachment_preserves_board_location(tmp_path, monkeypatch):
    pins = {"HERMES_KANBAN_DB": str(tmp_path / "chosen.db"),
            "HERMES_KANBAN_HOME": str(tmp_path), "HERMES_KANBAN_BOARD": "chosen"}
    for key, value in pins.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, str(tmp_path))
    monkeypatch.setenv(dc.DELEGATED_CHILD_OWNER_ENV, "99999999")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_owned")
    monkeypatch.setattr(dc, "_parent_pid", lambda pid: 0)
    dc._live_cache.clear()
    try:
        assert not dc.is_delegated_child_process_context()
        assert not os.environ.get("HERMES_KANBAN_TASK")
        assert {key: os.environ.get(key) for key in pins} == pins
        assert kb.kanban_db_path() == tmp_path / "chosen.db"
    finally:
        dc._live_cache.clear()
