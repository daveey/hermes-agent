"""Durable continuation is scoped to a board, task, run and profile, never guessed."""
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worker_recovery as recovery
from run_agent import AIAgent


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    board = tmp_path / "kanban.db"
    conn = sqlite3.connect(board)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, assignee TEXT);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER,
            claim_expires INTEGER, worker_pid INTEGER, profile TEXT, outcome TEXT);
    """)
    expiry = int(time.time()) + 3600
    conn.execute("INSERT INTO tasks VALUES ('t_one','running',1,'lock',?,?,'default')", (expiry, os.getpid()))
    conn.execute("INSERT INTO task_runs VALUES (1,'t_one',NULL,?,?,'default',NULL)", (expiry, os.getpid()))
    conn.commit()
    state_path = tmp_path / "state.db"
    state = sqlite3.connect(state_path)
    state.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, active INTEGER DEFAULT 1, _compressed_summary INTEGER DEFAULT 0);
        INSERT INTO sessions VALUES ('worker','kanban','default');
        INSERT INTO messages(session_id,role,content) VALUES ('worker','assistant','Implemented parser; next run tests');
    """)
    state.commit()
    for key, value in {"DB": str(board), "TASK": "t_one", "RUN_ID": "1", "CLAIM_LOCK": "lock"}.items():
        monkeypatch.setenv("HERMES_KANBAN_" + key, value)
    yield SimpleNamespace(conn=conn, state=state, db_path=state_path, board=board)
    state.close()
    conn.close()


def close_run(worker, outcome="crashed"):
    worker.conn.execute("UPDATE task_runs SET ended_at=1,outcome=? WHERE id=1", (outcome,))
    worker.conn.execute("UPDATE tasks SET status='ready',current_run_id=NULL,claim_lock=NULL")
    worker.conn.commit()


@pytest.mark.parametrize("outcome", ["crashed", "reclaimed", "failed", "max_iterations"])
def test_crash_and_budget_continuation_survives_reopen(worker, outcome):
    assert recovery.register_worker_session(worker, "worker")
    close_run(worker, outcome)
    with sqlite3.connect(worker.board) as reopened:
        result = recovery.build_recovery_context(reopened, "t_one")
    assert "Implemented parser; next run tests" in result
    assert outcome in result
    assert "NEW run's claim" in result
    assert "does not increase" in result


def test_open_run_is_not_its_own_recovery(worker):
    assert recovery.register_worker_session(worker, "worker")
    assert recovery.build_recovery_context(worker.conn, "t_one") == ""


@pytest.mark.parametrize("mutation", [
    "UPDATE tasks SET current_run_id=2",
    "UPDATE tasks SET claim_lock='other'",
    "UPDATE tasks SET status='ready'",
    "UPDATE tasks SET claim_expires=0",
    "UPDATE tasks SET worker_pid=-1",
    "UPDATE task_runs SET ended_at=1",
    "UPDATE task_runs SET claim_expires=0",
    "UPDATE task_runs SET worker_pid=-1",
    "UPDATE task_runs SET profile='foreign'",
    "UPDATE task_runs SET task_id='other'",
])
def test_stale_or_foreign_claim_cannot_register(worker, mutation):
    worker.conn.execute(mutation)
    worker.conn.commit()
    assert not recovery.register_worker_session(worker, "worker")


@pytest.mark.parametrize("key,value", [("RUN_ID", "01"), ("RUN_ID", "bad"), ("TASK", "other"), ("CLAIM_LOCK", "")])
def test_exact_pins_required(worker, monkeypatch, key, value):
    monkeypatch.setenv("HERMES_KANBAN_" + key, value)
    assert not recovery.register_worker_session(worker, "worker")


@pytest.mark.parametrize("mutation", [
    "UPDATE sessions SET source='cli'", "UPDATE sessions SET profile_name='foreign'",
    "UPDATE sessions SET profile_name=NULL",
])
def test_unrelated_sessions_rejected(worker, mutation):
    worker.state.execute(mutation)
    worker.state.commit()
    assert not recovery.register_worker_session(worker, "worker")


def test_delegates_and_foreign_store_rejected(worker, monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert not recovery.register_worker_session(worker, "worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    assert not recovery.register_worker_session(SimpleNamespace(db_path=worker.board), "worker")


def test_idempotent_but_cannot_rebind_to_another_run(worker, monkeypatch):
    assert recovery.register_worker_session(worker, "worker")
    assert recovery.register_worker_session(worker, "worker")
    worker.conn.execute("INSERT INTO task_runs SELECT 2,task_id,NULL,claim_expires,worker_pid,profile,NULL FROM task_runs WHERE id=1")
    worker.conn.execute("UPDATE tasks SET current_run_id=2")
    worker.conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "2")
    assert not recovery.register_worker_session(worker, "worker")


def test_board_task_profile_isolation_and_no_origin_fallback(worker):
    assert recovery.register_worker_session(worker, "worker")
    close_run(worker)
    assert recovery.build_recovery_context(worker.conn, "t_other") == ""
    with sqlite3.connect(":memory:") as other_board:
        assert recovery.build_recovery_context(other_board, "t_one") == ""
    worker.conn.execute("UPDATE tasks SET assignee='foreign'")
    assert recovery.build_recovery_context(worker.conn, "t_one") == ""


def test_missing_or_retagged_transcript_fails_closed(worker):
    assert recovery.register_worker_session(worker, "worker")
    close_run(worker)
    worker.state.execute("UPDATE sessions SET profile_name='foreign'")
    worker.state.commit()
    assert recovery.build_recovery_context(worker.conn, "t_one") == ""
    worker.db_path.unlink()
    assert recovery.build_recovery_context(worker.conn, "t_one") == ""
    assert not worker.db_path.exists()


def test_tail_bounded_excludes_system_user_reasoning_and_inactive(worker):
    assert recovery.register_worker_session(worker, "worker")
    worker.state.executemany(
        "INSERT INTO messages(session_id,role,content) VALUES ('worker','tool',?)",
        [(f"output {i} " + "x" * 10000,) for i in range(40)],
    )
    worker.state.execute("INSERT INTO messages(session_id,role,content) VALUES ('worker','assistant','CONTINUE_FROM_HERE')")
    worker.state.execute("INSERT INTO messages(session_id,role,content,active) VALUES ('worker','tool','INACTIVE_SECRET',0)")
    worker.state.execute("INSERT INTO messages(session_id,role,content) VALUES ('worker','system','SYSTEM_SECRET')")
    worker.state.execute("INSERT INTO messages(session_id,role,content) VALUES ('worker','user','USER_SECRET')")
    worker.state.commit()
    close_run(worker)
    result = recovery.build_recovery_context(worker.conn, "t_one")
    assert len(result) <= recovery.MAX_CONTEXT_CHARS + 1
    assert "CONTINUE_FROM_HERE" in result
    assert "output 0 " not in result
    assert "SECRET" not in result


def test_real_schema_lazy_agent_registration_and_reclaim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_state import SessionDB


    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    path = tmp_path / "board.db"
    kbc.init_db(path)
    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Continue parser", assignee="default")
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        assert task is not None
        for key, value in {"DB": str(path), "TASK": tid, "RUN_ID": str(task.current_run_id), "CLAIM_LOCK": task.claim_lock}.items():
            monkeypatch.setenv("HERMES_KANBAN_" + key, value)
        state = SessionDB(db_path=tmp_path / "state.db")
        try:
            agent = SimpleNamespace(
                _session_db=state, _session_db_created=False, session_id="real-worker",
                platform="cli", model="test", _cached_system_prompt="", _parent_session_id=None,
                _session_row_model_config=dict,
            )
            AIAgent._ensure_db_session(agent)
            assert agent._session_db_created
            assert conn.execute("SELECT session_id FROM worker_recovery_sessions").fetchone()[0] == "real-worker"
            state.append_message("real-worker", "assistant", "Budget reached; parser complete, run tests next")
            conn.execute("UPDATE tasks SET claim_expires=0 WHERE id=?", (tid,))
            conn.commit()
            kb.release_stale_claims(conn)
            context = recovery.build_recovery_context(conn, tid)
            assert "parser complete, run tests next" in context
            new_task = kb.claim_task(conn, tid)
            assert new_task is not None
            assert "parser complete, run tests next" in recovery.build_recovery_context(conn, tid)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(new_task.current_run_id))
            monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", new_task.claim_lock)
            from tools import kanban_tools
            shown = json.loads(kanban_tools._handle_show({}))
            assert "parser complete, run tests next" in shown["worker_context"]
        finally:
            state.close()


def test_compression_rotation_uses_latest_registered_session(worker):
    assert recovery.register_worker_session(worker, "worker")
    worker.state.execute("INSERT INTO sessions VALUES ('compressed','kanban','default')")
    worker.state.execute("INSERT INTO messages(session_id,role,content,_compressed_summary) VALUES ('compressed','user','Compressed progress and next actions',1)")
    worker.state.commit()
    assert recovery.register_worker_session(worker, "compressed")
    close_run(worker)
    result = recovery.build_recovery_context(worker.conn, "t_one")
    assert "Compressed progress and next actions" in result
    assert "Implemented parser" not in result
