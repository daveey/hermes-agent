"""Receipt admission through every real entry path that reaches canonical ``complete_task``.

Direct tool handler, the wrapped ``tool_call`` bridge, the in-process CLI and a real
``hermes kanban complete`` subprocess (the only way ``execute_code`` reaches the board:
its sandbox exposes ``terminal``, never kanban tools). Anonymous synthetic fixtures.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hermes_state.test_completion_receipt import receipt

REPO = Path(__file__).resolve().parents[2]
MALFORMED = {"version": 1, "items": [{**receipt("superseded")["items"][0], "superseded_by": None}]}


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _task(kb, monkeypatch=None, claim=True):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Bounded assessment", assignee="test-worker")
        if claim:
            kb.claim_task(conn, tid)
            run_id = kb._current_run_id(conn, tid)
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _state(kb, tid):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        status = kb.get_task(conn, tid).status
        events = [(r[0], r[1]) for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        runs = [tuple(r) for r in conn.execute("SELECT id, status, outcome, metadata FROM task_runs WHERE task_id=?",
                                               (tid,))]
    return status, events, runs


def _stored_receipt(kb, tid):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_completion_receipt_projection import read_completed
    with kbc.connect() as conn:
        return read_completed(conn, tid, gateway="src", board="default")


def _args(value):
    return {"summary": "Assessment finished; artifact rejected.", "metadata": {"completion_receipt": value}}


def _direct(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_complete(args))


def _bridge(args):
    import model_tools
    out = model_tools.handle_function_call(
        "tool_call", {"calls": [{"name": "kanban_complete", "arguments": args}]},
        skip_pre_tool_call_hook=True)
    return json.loads(out)


@pytest.fixture
def forwarding(monkeypatch):
    """`tool_search.forward_direct_calls`: the bridge runs a directly-listed tool."""
    import dataclasses

    from tools import tool_search as ts
    real = ts.load_config_readonly
    monkeypatch.setattr(ts, "load_config_readonly",
                        lambda *a, **k: dataclasses.replace(real(*a, **k), forward_direct_calls=True))


def _wrapped(args):
    return _bridge(args)


def test_default_tool_call_bridge_never_invokes_completion(board, monkeypatch):
    tid = _task(board, monkeypatch)
    before = _state(board, tid)
    out = _bridge(_args(receipt("rejected")))
    assert "NOT invoked" in out["error"]
    assert _state(board, tid) == before


@pytest.fixture(params=["direct", "tool_call"])
def call(request):
    if request.param == "tool_call":
        request.getfixturevalue("forwarding")
        return _wrapped
    return _direct


def test_tool_paths_reject_malformed_receipt_atomically(board, monkeypatch, call):
    tid = _task(board, monkeypatch)
    before = _state(board, tid)
    out = call(_args(MALFORMED))
    assert out.get("error") and "completion_receipt" in json.dumps(out), out
    assert _state(board, tid) == before


def test_tool_paths_store_admitted_rejected_receipt(board, monkeypatch, call):
    tid = _task(board, monkeypatch)
    value = receipt("rejected")
    out = call(_args(value))
    assert not out.get("error"), out
    projected = _stored_receipt(board, tid)
    assert projected["completion_receipt"] == value
    assert projected["completion_receipt_source"]["ref"] == f"@task:default@src/{tid}"


def _cli(tid, value):
    from hermes_cli import kanban as cli
    return cli._cmd_complete(argparse.Namespace(
        task_ids=[tid], summary="Assessment finished; artifact rejected.", result=None,
        metadata=json.dumps({"completion_receipt": value}), force=False))


def test_cli_rejects_malformed_receipt_without_write(board, monkeypatch, capsys):
    tid = _task(board, claim=False)
    before = _state(board, tid)
    assert _cli(tid, MALFORMED) != 0
    assert "completion_receipt" in capsys.readouterr().err
    assert _state(board, tid) == before


def test_cli_stores_admitted_receipt(board, monkeypatch):
    tid = _task(board, claim=False)
    value = receipt("contained")
    assert _cli(tid, value) == 0
    assert _stored_receipt(board, tid)["completion_receipt"] == value


def _subprocess(tid, value, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
    env.update(HERMES_HOME=os.environ["HERMES_HOME"], HOME=str(tmp_path), HERMES_PROFILE="test-worker")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "complete", tid,
         "--summary", "Assessment finished.", "--metadata", json.dumps({"completion_receipt": value})],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240, check=False)


def test_terminal_subprocess_path_rejects_then_admits(board, tmp_path):
    """execute_code -> terminal -> `hermes kanban complete`: same canonical admission."""
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS
    assert not {n for n in SANDBOX_ALLOWED_TOOLS if n.startswith("kanban")}
    tid = _task(board, claim=False)
    before = _state(board, tid)
    bad = _subprocess(tid, MALFORMED, tmp_path)
    assert bad.returncode != 0 and "completion_receipt" in bad.stderr, bad
    assert "Traceback" not in bad.stderr
    assert _state(board, tid) == before
    value = receipt("superseded")
    good = _subprocess(tid, value, tmp_path)
    assert good.returncode == 0, good
    assert _stored_receipt(board, tid)["completion_receipt"] == value


def test_malformed_receipt_refused_before_goal_judge(board, monkeypatch):
    """No judge call (and so no judge-driven park) for a structurally invalid receipt."""
    from hermes_cli import kanban as cli
    from tools import kanban_tools as kt

    def judge(*_a, **_k):
        raise AssertionError("goal judge consulted before receipt validation")

    monkeypatch.setattr(kt, "_goal_gate", judge)
    monkeypatch.setattr(cli, "_goal_gate_error", judge)
    tid = _task(board, monkeypatch)
    before = _state(board, tid)
    assert "completion_receipt" in _direct(_args(MALFORMED))["error"]
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    assert _cli(tid, MALFORMED) != 0
    assert _state(board, tid) == before
