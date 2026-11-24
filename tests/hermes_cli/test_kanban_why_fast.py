"""Real snapshot CLI import budget, with a real dispatcher-produced snapshot."""
import json
import os
from pathlib import Path
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_governors as governors
from tests.hermes_cli import test_kanban_governors as helpers

board = helpers.board
clean = helpers.clean


def test_snapshot_cli_does_not_import_dispatch(board, monkeypatch):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="snapshot import guard", assignee="default")
        helpers._tick(conn)
        recorded = governors.card(conn, tid)
        assert recorded and recorded["governor"] == "llm_pool"
        from hermes_cli.kanban_why_fast import read_answer
        path = governors._db_path(conn)
        assert read_answer(path, tid), recorded
    env = dict(os.environ)
    env["HERMES_KANBAN_HOME"] = str(board)
    for key in list(env):
        if key.startswith("HERMES_KANBAN_") and key != "HERMES_KANBAN_HOME":
            env.pop(key)
    code = """import runpy, sys
try:
    runpy.run_module('hermes_cli.main', run_name='__main__')
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
for name in ('hermes_cli.kanban', 'hermes_cli.kanban_db', 'hermes_cli.kanban_db_dispatch',
             'hermes_cli.kanban_governors', 'hermes_cli.kanban_governors_explain'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, "-c", code, "kanban", "why", tid, "--board", "default", "--json"],
                            cwd=Path(__file__).resolve().parents[2], env=env, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["source"] == "snapshot"
    assert answer["first_stop"]["governor"] == "llm_pool"
    assert answer["first_stop"]["label"]
    assert answer["first_stop"]["source"]


def test_snapshot_reader_fallbacks_and_parity(board, monkeypatch):
    from hermes_cli import kanban_why_fast as fast
    from hermes_cli import kanban_governors_cli as cli

    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reader parity", assignee="default")
        helpers._tick(conn)
        path = governors._db_path(conn)
        snap_path = governors.snapshot_path(conn)
        original = json.loads(snap_path.read_text())
        expected = cli.why(conn, tid)
        actual = fast.read_answer(path, tid)
        assert actual is not None
        for result in (expected, actual):
            result.pop("elapsed_s")
            result["first_stop"].pop("age_s")
            result["recorded"].pop("age_s")
        assert actual == expected
        for update in ({"version": 999}, {"tick_at": 0}, {"tick_at": 1e20},
                       {"cards": {}}, {"cards": []}, {"tick_at": "invalid"}):
            snap_path.write_text(json.dumps({**original, **update}))
            assert fast.read_answer(path, tid) is None, update
        old = json.loads(json.dumps(original))
        old["cards"][tid].pop("source")
        snap_path.write_text(json.dumps(old))
        assert fast.read_answer(path, tid) is None
        snap_path.write_text("not json")
        assert fast.read_answer(path, tid) is None
        snap_path.unlink()
        assert fast.read_answer(path, tid) is None
        snap_path.write_text(json.dumps(original))
        assert fast.read_answer(path, "missing") is None
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.commit()
        assert fast.read_answer(path, tid) is None


def test_fast_cli_defers_fences_and_unsupported_flags(monkeypatch, tmp_path):
    from hermes_cli import kanban_why_fast as fast

    def unexpected(*args, **kwargs):
        raise AssertionError("must defer before reading")

    monkeypatch.setattr(fast, "read_answer", unexpected)
    for key in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        with monkeypatch.context() as m:
            m.setenv(key, "fenced")
            assert fast.try_cli(["kanban", "why", "task", "--json"]) is False
    for args in (["kanban", "why", "task"], ["kanban", "why", "task", "--json", "--live"],
                 ["kanban", "why", "task", "--json", "--unknown"],
                 ["kanban", "why", "task", "--board", "../escape", "--json"]):
        assert fast.try_cli(args) is False
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "kanban").mkdir()
    (tmp_path / "kanban" / "current").write_text("other")
    assert fast.try_cli(["kanban", "why", "task", "--json"]) is False


def test_fast_entry_preserves_container_routing(monkeypatch):
    from hermes_cli import _startup_fast as startup
    from hermes_cli import kanban_why_fast as fast

    calls = []
    monkeypatch.setattr(fast, "try_cli", lambda args: calls.append(args) or True)
    monkeypatch.setattr(startup, "container_mode_may_be_active", lambda: True)
    args = ["kanban", "why", "task", "--json"]
    assert startup.try_fast_readonly(args) is False
    assert calls == []
    monkeypatch.setattr(startup, "container_mode_may_be_active", lambda: False)
    assert startup.try_fast_readonly(args) is True
    assert calls == [args]
