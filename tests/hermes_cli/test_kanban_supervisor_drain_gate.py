"""Quiet-board supervisor gates retain request and process-driven work. [daveey]"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_operator_call as kox
from hermes_cli import kanban_supervisor_requests as sr
from hermes_cli import kanban_sweep_gate as gate
from tests.hermes_cli import test_kanban_supervisor_requests as helpers

board = helpers.board
_APPLY_INCOMING = kox.apply_incoming


@pytest.fixture
def executor(board, monkeypatch):
    helpers._as_executor(monkeypatch)
    gate.reset()
    monkeypatch.setattr(kox, "apply_incoming", Mock(return_value=[]))
    with kbc.connect_closing() as conn:
        sr.ensure_schema(conn)
        conn.commit()
        yield conn
    gate.reset()


def test_idle_ticks_skip_config_and_local_drain(executor, monkeypatch):
    from hermes_cli import config
    load = Mock(return_value={})
    monkeypatch.setattr(config, "load_config_readonly", load)
    real = sr.drain
    drain = Mock(wraps=real)
    monkeypatch.setattr(sr, "drain", drain)
    for _ in range(4):
        sr.dispatcher_drain(executor)
    assert drain.call_count == 1
    assert load.call_count == 1
    # Incoming replica requests and label-process completions are an independent lane.
    assert kox.apply_incoming.call_count == 4


def test_request_only_insert_on_settled_board_runs_next_tick(executor, board):
    for _ in range(3):
        sr.dispatcher_drain(executor)
    before = gate.content_revision(executor)
    executor.execute(
        "INSERT INTO supervisor_requests(requester_task_id, ops_json, dedupe_key, created_at) "
        "VALUES (?, '[]', 'quiet', 0)", (board["requester"],))
    executor.commit()
    assert gate.content_revision(executor) == before
    result = sr.dispatcher_drain(executor)
    assert result and result[0][1] == "rejected"


def test_executor_refresh_due_without_content_change(executor, monkeypatch):
    clock = Mock(return_value=1000)
    monkeypatch.setattr(sr.time, "time", clock)
    sr.dispatcher_drain(executor)
    clock.return_value = 1060
    sr.dispatcher_drain(executor)
    assert executor.execute("SELECT last_seen FROM supervisor_executors").fetchone()[0] == 1000
    clock.return_value = 1061
    sr.dispatcher_drain(executor)
    assert executor.execute("SELECT last_seen FROM supervisor_executors").fetchone()[0] == 1061


def test_failed_drain_retries_unchanged_key(executor, monkeypatch):
    real = sr.drain
    calls = Mock(side_effect=[RuntimeError("retry"), real(executor)])
    monkeypatch.setattr(sr, "drain", calls)
    sr.dispatcher_drain(executor)
    sr.dispatcher_drain(executor)
    assert calls.call_count == 2


def test_live_holder_killed_on_quiet_board_fails_next_tick(executor, board):
    import socket
    import subprocess
    import sys
    from tools.environments.local import build_subprocess_env

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             env=build_subprocess_env())
    try:
        executor.execute(
            "INSERT INTO supervisor_requests(requester_task_id,ops_json,dedupe_key,created_at,"
            "status,executor,lease_expires,attempts) VALUES (?, '[]', 'holder', ?, 'accepted', ?, ?, ?)",
            (board["requester"], int(sr.time.time()), f"{socket.gethostname()}:{child.pid}:test",
             int(sr.time.time()) + 1000, sr.MAX_ATTEMPTS))
        executor.commit()
        for _ in range(3):
            assert sr.dispatcher_drain(executor) == []
        before = gate.content_revision(executor)
        child.kill()
        child.wait(timeout=10)
        assert gate.content_revision(executor) == before
        assert sr.dispatcher_drain(executor) == [(1, "failed")]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_expiry_reached_without_write(executor, board, monkeypatch):
    clock = Mock(return_value=1000)
    monkeypatch.setattr(sr.time, "time", clock)
    executor.execute(
        "INSERT INTO supervisor_requests(requester_task_id,ops_json,dedupe_key,created_at,"
        "status,executor,lease_expires,attempts) VALUES (?, '[]', 'expiry', ?, 'accepted', ?, ?, 0)",
        (board["requester"], 1000 - sr.DEADLINE_S, sr.executor_identity("test"), 2000))
    executor.commit()
    for _ in range(3):
        assert sr.dispatcher_drain(executor) == []
    before = gate.content_revision(executor)
    clock.return_value = 1001
    assert gate.content_revision(executor) == before
    assert sr.dispatcher_drain(executor) == [(1, "rejected")]


def test_request_insert_accepts_next_tick(executor, board, monkeypatch):
    import json
    for _ in range(3):
        sr.dispatcher_drain(executor)
    accepted = Mock()
    monkeypatch.setattr(sr, "_after_accept", accepted)
    before = gate.content_revision(executor)
    executor.execute(
        "INSERT INTO supervisor_requests(requester_task_id,ops_json,dedupe_key,created_at) VALUES (?, ?, 'new', ?)",
        (board["requester"], json.dumps(helpers._reorder(board)), int(sr.time.time())))
    executor.commit()
    assert gate.content_revision(executor) == before
    assert sr.dispatcher_drain(executor) == [(1, "done")]
    accepted.assert_called_once()


def test_config_change_reenables_pending_requests(executor, board, monkeypatch):
    from hermes_cli import config
    key = Mock(return_value="disabled")
    monkeypatch.setattr(gate, "config_key", key)
    load = Mock(return_value={"kanban": {"supervisor_requests": {"enabled": False}}})
    monkeypatch.setattr(config, "load_config_readonly", load)
    executor.execute(
        "INSERT INTO supervisor_requests(requester_task_id,ops_json,dedupe_key,created_at) VALUES (?, '[]', 'new', 0)",
        (board["requester"],))
    executor.commit()
    assert sr.dispatcher_drain(executor) == []
    assert sr.dispatcher_drain(executor) == []
    assert load.call_count == 1
    load.return_value = {}
    key.return_value = "enabled"
    assert sr.dispatcher_drain(executor) == [(1, "rejected")]


def test_terminal_request_update_invalidates_without_task_write(executor, board, monkeypatch):
    executor.execute(
        "INSERT INTO supervisor_requests(requester_task_id,ops_json,dedupe_key,created_at,status,requester_woken) "
        "VALUES (?, '[]', 'late', 0, 'done', 1)", (board["requester"],))
    executor.commit()
    for _ in range(3):
        sr.dispatcher_drain(executor)
    before = gate.content_revision(executor)
    drain = Mock(wraps=sr.drain)
    monkeypatch.setattr(sr, "drain", drain)
    executor.execute("UPDATE supervisor_requests SET requester_woken=0")
    executor.commit()
    assert gate.content_revision(executor) == before
    sr.dispatcher_drain(executor)
    drain.assert_called_once()


def test_unstubbed_drain_replay(executor, monkeypatch, tmp_path):
    import json
    import runpy
    from pathlib import Path
    # The benchmark deliberately leaves poll/apply_incoming real. The fixture has no
    # gateway/replica configured, so incoming delivery is outside this replay's scope.
    monkeypatch.setattr(kox, "apply_incoming", _APPLY_INCOMING)
    script = Path(__file__).resolve().parents[2] / "scripts/kanban_supervisor_replay_bench.py"
    replay = runpy.run_path(str(script))["replay"]
    report = replay(executor, 40)
    (tmp_path / "supervisor-replay.json").write_text(json.dumps(report), encoding="utf-8")
    assert report["ungated"]["executor_upserts"] == 40
    assert report["gated"]["executor_upserts"] == 8
    assert report["gated"]["last_seen"] == report["ungated"]["last_seen"]
