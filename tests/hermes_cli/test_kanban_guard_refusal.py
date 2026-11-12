"""A clean exit whose last Kanban calls a host guard refused is a host refusal, not a protocol violation.

2026-11-12, David-MBP: two runs of t_01a1ce9070eee14c had every tool call refused by a skipped
``pre_tool_call`` guard (``kanban_continue`` / ``kanban_comment`` included), ended their turn with a text
summary (rc=0), and were booked protocol violations: counted, with a ✖ notice. The worker tried to park.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_worker_recovery import _SCHEMA as RECOVERY_SCHEMA
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

# Mirrors kanban_guard_refusal.TAIL_TOOL_RESULTS / STREAK_LIMIT (imported lazily so the RED run on a base
# without the module still runs every test).
TAIL_TOOL_RESULTS = 6
STREAK_LIMIT = 3

REFUSAL = json.dumps({"error": "pre_tool_call plugin callback timed out or is still running "
                               "(plugin hermes_plugins.slow_guard, callback _pre_tool_call); retry in 41 s"})


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _session(home: Path, conn, tid: str, run_id: int, tool_results: list[tuple[str, str]]) -> None:
    """Register a worker session for ``run_id`` whose tool results are ``tool_results`` (oldest first)."""
    state_db = home / "state.db"
    with sqlite3.connect(state_db) as state:
        state.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                      "tool_name TEXT, content TEXT)")
        state.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', 'working')",
                      (f"s-{run_id}",))
        for name, content in tool_results:
            state.execute("INSERT INTO messages (session_id, role, tool_name, content) VALUES (?, 'tool', ?, ?)",
                          (f"s-{run_id}", name, content))
    conn.execute(RECOVERY_SCHEMA)
    conn.execute("INSERT INTO worker_recovery_sessions VALUES (?, ?, 'default', ?, ?, ?)",
                 (run_id, tid, str(state_db), f"s-{run_id}", time.time()))
    conn.commit()


def _clean_exit(conn, tid: str, pid: int) -> int:
    """Claim ``tid`` for a worker that already exited 0 (logged trailer); returns its run id."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
                 (pid, int(time.time()) - 120, tid))
    conn.commit()
    with kbd._open_worker_log(kb.get_task(conn, tid), None) as f:
        f.write(f"Parked; summary follows.\n\n{KANBAN_WORKER_EXIT_TRAILER}0\n".encode())
    return kb.get_task(conn, tid).current_run_id


def _booking(conn, tid):
    run = conn.execute("SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    return run, kinds, kb.get_task(conn, tid)


REFUSED_TAIL = [("terminal", REFUSAL), ("kanban_heartbeat", REFUSAL), ("kanban_continue", REFUSAL),
                ("kanban_comment", REFUSAL)]


def test_refused_kanban_calls_requeue_uncounted_without_notice(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        run_id = _clean_exit(conn, tid, 70101)
        _session(kanban_home, conn, tid, run_id, REFUSED_TAIL)

        kbd.detect_crashed_workers(conn)

        run, kinds, task = _booking(conn, tid)
        meta = json.loads(run["metadata"])
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert run["outcome"] == "reclaimed"
        assert meta["guard_refusal"] == {"refused_tools": ["kanban_heartbeat", "kanban_continue", "kanban_comment"]}
        assert "protocol_violation" not in meta
        assert "host refusal" in run["error"] and "kanban_continue" in run["error"]
        # No ✖: neither the crash nor the protocol-violation failure notice is emitted.
        assert "failure_finalized" not in kinds and "crashed" not in kinds and "protocol_violation" not in kinds
        assert "owner_restart_lost" in kinds
        assert kbd._protocol_violation_streak(conn, tid) == 0


def test_run_without_kanban_calls_stays_a_protocol_violation(kanban_home):
    """Control: a run that never attempted a Kanban call (only ordinary refused/ok tools) is still the
    worker's protocol violation."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        run_id = _clean_exit(conn, tid, 70102)
        _session(kanban_home, conn, tid, run_id, [("terminal", REFUSAL), ("read_file", '{"content": "x"}')])

        kbd.detect_crashed_workers(conn)

        run, kinds, _task = _booking(conn, tid)
        assert json.loads(run["metadata"]).get("protocol_violation") is True
        assert "protocol_violation" in kinds and "failure_finalized" in kinds
        assert kbd._protocol_violation_streak(conn, tid) == 1


def test_old_refusal_outside_the_tail_stays_a_protocol_violation(kanban_home):
    """A refusal long before the run ended (followed by ordinary work) is not how the run ended."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        run_id = _clean_exit(conn, tid, 70103)
        tail = [("kanban_comment", REFUSAL)] + [("read_file", '{"content": "x"}')] * TAIL_TOOL_RESULTS
        _session(kanban_home, conn, tid, run_id, tail)

        kbd.detect_crashed_workers(conn)

        run, kinds, _task = _booking(conn, tid)
        assert json.loads(run["metadata"]).get("protocol_violation") is True
        assert "failure_finalized" in kinds


def test_refusal_streak_is_bounded(kanban_home):
    """A guard that stays hung must not requeue a card uncounted forever: after ``STREAK_LIMIT`` such runs
    the next one is charged as a protocol violation again."""
    from hermes_cli import kanban_guard_refusal as gr

    assert (gr.TAIL_TOOL_RESULTS, gr.STREAK_LIMIT) == (TAIL_TOOL_RESULTS, STREAK_LIMIT)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        for n in range(STREAK_LIMIT + 1):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            conn.commit()
            run_id = _clean_exit(conn, tid, 70200 + n)
            _session(kanban_home, conn, tid, run_id, REFUSED_TAIL)
            kbd.detect_crashed_workers(conn)
            run, _kinds, _task = _booking(conn, tid)
            meta = json.loads(run["metadata"])
            if n < STREAK_LIMIT:
                assert meta.get("guard_refusal") and run["outcome"] == "reclaimed", n
            else:
                assert meta.get("protocol_violation") is True and meta.get("guard_refusal_streak") == STREAK_LIMIT
