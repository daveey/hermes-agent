"""Checkpoint + continue instead of dying at ``max_runtime_seconds``.

One test per acceptance path:
  1. a worker reaching the soft deadline is steered, checkpoints with kanban_continue, and the
     re-queued run resumes from that checkpoint (plus the prior run's transcript tail);
  2. a worker that ignores the soft deadline is still hard-killed at the cap, and its retry
     carries the transcript context;
  3. continuations never trip the give-up breaker or the unblock-loop counter (they have their
     own bound).
Plus the wait hand-off (``resume_after_seconds``) waking through the dispatcher tick.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
import tools.kanban_tools as kt

# A scheduled wait needs a wake reason (kanban_awaiting no_wake_reason).
REMEASURE = {"kind": "clock", "why": "the scheduled re-measure"}


class FakeAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kc, "soft_deadline_fraction", lambda cfg=None: 0.85)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kt._soft_deadline_sent.clear()
    kt._soft_deadline_last_attempt = 0.0
    return home


def _start_run(monkeypatch, tid: str, *, started_ago: int) -> int:
    """Claim like the dispatcher, pin the worker env, backdate the active run."""
    with kbc.connect_closing() as conn:
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, os.getpid())
        run_id = kb._current_run_id(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?",
                         (int(time.time()) - started_ago, run_id))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    kt._soft_deadline_last_attempt = 0.0
    return run_id


def _register_transcript(home: Path, tid: str, run_id: int, text: str) -> None:
    """What b78e9861e6's register_worker_session leaves behind for a worker session."""
    state = home / "state.db"
    with sqlite3.connect(state) as s:
        s.executescript(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT);"
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_calls TEXT, active INTEGER DEFAULT 1, _compressed_summary INTEGER DEFAULT 0);")
        sid = f"sess-{run_id}"
        s.execute("INSERT INTO sessions VALUES (?, 'kanban', 'default')", (sid,))
        s.execute("INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', ?)", (sid, text))
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS worker_recovery_sessions (run_id INTEGER NOT NULL, task_id TEXT NOT NULL,"
                " profile TEXT NOT NULL, state_db TEXT NOT NULL, session_id TEXT NOT NULL, registered_at REAL NOT NULL,"
                " PRIMARY KEY (state_db, session_id))")
            conn.execute("INSERT INTO worker_recovery_sessions VALUES (?, ?, 'default', ?, ?, ?)",
                         (run_id, tid, str(state), sid, time.time()))


def test_soft_deadline_checkpoint_requeues_and_next_run_resumes(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long build", assignee="default", max_runtime_seconds=1000)

    # Before the soft deadline: nothing is injected.
    run1 = _start_run(monkeypatch, tid, started_ago=500)
    agent = FakeAgent()
    assert kt.inject_soft_deadline_from_env(agent) is False
    assert agent.steers == []

    # At 85%: one steer naming kanban_continue; not repeated on the next poll.
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (int(time.time()) - 860, run1))
    kt._soft_deadline_last_attempt = 0.0
    assert kt.inject_soft_deadline_from_env(agent) is True
    assert "kanban_continue" in agent.steers[0] and "soft deadline" in agent.steers[0]
    kt._soft_deadline_last_attempt = 0.0
    assert kt.inject_soft_deadline_from_env(agent) is False

    # The worker checkpoints through the real tool handler.
    _register_transcript(home, tid, run1, "Built stage 1 of 3; stage 2 needs the cache in /tmp/c")
    checkpoint = "Done: stage 1 (commit abc123).\nNext: stage 2. Re-adopt pid 4242 (log /tmp/b.log)."
    out = json.loads(kt._handle_continue({"checkpoint": checkpoint}))
    assert out["ok"] is True and out["status"] == "ready" and out["continuation"] == 1

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.worker_pid, task.claim_lock) == ("ready", None, None)
        run = kb.latest_run(conn, tid)
        assert (run.id, run.outcome) == (run1, "continued")
        assert run.metadata["checkpoint"] == checkpoint
        assert any(e.kind == "continued" for e in kb.list_events(conn, tid))
        # Re-queued for the very next tick: the dispatcher can claim it again right away.
        assert kb.claim_task(conn, tid) is not None
        context = kb.build_worker_context(conn, tid)
    assert f"## Continuation checkpoint (from run {run1}, continuation #1)" in context
    assert "Re-adopt pid 4242" in context
    # b78e9861e6 transcript recovery rides along.
    assert "Built stage 1 of 3" in context and "outcome continued" in context
    # The checkpoint sits above the transcript tail, right after the body.
    assert context.index("Continuation checkpoint") < context.index("Interrupted worker continuation")


def test_ignoring_soft_deadline_is_hard_killed_and_retry_carries_context(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="stubborn", assignee="default", max_runtime_seconds=100)
    run1 = _start_run(monkeypatch, tid, started_ago=96)
    agent = FakeAgent()
    assert kt.inject_soft_deadline_from_env(agent) is True       # warned...
    assert "FINAL WARNING" in agent.steers[0]                     # (96s >= final threshold 95s of a 100s cap)
    _register_transcript(home, tid, run1, "Still polling CI run 777 every 5 minutes")

    # ...ignored it; the cap passes and the dispatcher kills as before.
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (int(time.time()) - 130, run1))
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        killed = []
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append(pid)) == [tid]
        assert killed == [os.getpid()]
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 1
        assert kb.latest_run(conn, tid).outcome == "timed_out"
        context = kb.build_worker_context(conn, tid)
    assert "Still polling CI run 777" in context and "outcome timed_out" in context
    assert "Continuation checkpoint" not in context   # no checkpoint was written


def test_continuations_do_not_trip_give_up_or_block_loop(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="marathon", assignee="default", max_runtime_seconds=600)
        with kb.write_txn(conn):
            # One earlier hard timeout and one earlier unblock cycle already on the card.
            conn.execute("UPDATE tasks SET consecutive_failures = 1, block_kind = 'transient', "
                         "block_recurrences = 1 WHERE id = ?", (tid,))

    for i in range(1, 6):
        _start_run(monkeypatch, tid, started_ago=550)
        out = json.loads(kt._handle_continue({"checkpoint": f"step {i} done"}))
        assert out["ok"] is True and out["continuation"] == i
        with kbc.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.consecutive_failures == 1     # breaker trips at 2: never reached
            assert (task.block_kind, task.block_recurrences) == ("transient", 1)
            assert not any(e.kind in ("gave_up", "block_loop_detected", "blocked")
                           for e in kb.list_events(conn, tid))
            assert kb.recompute_ready(conn) >= 0 and kb.get_task(conn, tid).status == "ready"

    # Continuations have their own bound instead.
    _start_run(monkeypatch, tid, started_ago=10)
    with kbc.connect_closing() as conn:
        res = kc.continue_task(conn, tid, checkpoint="again", max_continuations=5,
                               expected_run_id=int(os.environ["HERMES_KANBAN_RUN_ID"]))
        assert res["ok"] is False and "continuation limit" in res["error"]
        assert kb.get_task(conn, tid).status == "running"


def test_wait_handoff_wakes_on_dispatcher_tick(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ci wait", assignee="default", max_runtime_seconds=600)
    _start_run(monkeypatch, tid, started_ago=60)
    out = json.loads(kt._handle_continue({"checkpoint": "pushed abc; waiting for CI run 9",
                                          "resume_after_seconds": 900}))
    assert out["status"] == "scheduled" and out["wake_at"] >= int(time.time()) + 890
    with kbc.connect_closing() as conn:
        assert kc.wake_due_continuations(conn) == []                       # not due yet
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kc.wake_due_continuations(conn, now=out["wake_at"]) == [tid]
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 0)
        assert "waiting for CI run 9" in kb.build_worker_context(conn, tid)
        # A kanban_schedule park without a SCHEDULED_UNTIL marker still wakes, after the
        # default wait: no scheduled card may depend on a cron that can vanish. [daveey]
        tid2 = kb.create_task(conn, title="parked", assignee="default")
        kb.claim_task(conn, tid2)
        kb.schedule_task(conn, tid2, reason="scheduled re-measure", clock_only=True, awaiting=REMEASURE)
        assert kc.wake_due_continuations(conn, now=int(time.time()) + 3600) == []
        assert kc.wake_due_continuations(conn, now=int(time.time()) + 10 ** 7) == [tid2]


def _park_with_wait(monkeypatch, title: str) -> tuple[str, int]:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="default", max_runtime_seconds=600)
    _start_run(monkeypatch, tid, started_ago=60)
    out = json.loads(kt._handle_continue({"checkpoint": "waiting on CI", "resume_after_seconds": 900}))
    assert out["status"] == "scheduled"
    return tid, out["wake_at"]


@pytest.mark.parametrize("kind", ["commented", "heartbeat", "notified", "edited"])
def test_due_wake_is_not_masked_by_later_annotation(home, monkeypatch, kind):
    """A comment (another card's FYI), heartbeat or other non-transition event landing after
    the ``continued`` wait used to hide the wake forever."""
    tid, wake_at = _park_with_wait(monkeypatch, f"masked by {kind}")
    with kbc.connect_closing() as conn:
        if kind == "commented":
            kb.add_comment(conn, tid, "t_other", "FYI: parent landed")
        else:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, kind, {"note": "x"})
        assert kc.wake_due_continuations(conn, now=wake_at - 1) == []
        assert kc.wake_due_continuations(conn, now=wake_at) == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_kanban_schedule_after_wait_and_comment_still_stays_put(home, monkeypatch):
    """Re-parked with kanban_schedule (no marker) after a timed wait: it stays put for the
    default wait, even when a comment lands afterwards, then wakes. [daveey]"""
    from hermes_cli.kanban_timed_park import DEFAULT_WAKE_SECONDS
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="parked", assignee="default")
        kb.claim_task(conn, tid)
        kb.schedule_task(conn, tid, reason="scheduled re-measure", clock_only=True, awaiting=REMEASURE)
        kb.add_comment(conn, tid, "t_other", "FYI")
        assert kc.pending_wake(conn, tid)["wake_at"] >= int(time.time()) + DEFAULT_WAKE_SECONDS - 5
        assert kc.wake_due_continuations(conn, now=int(time.time()) + 3600) == []
        assert kb.get_task(conn, tid).status == "scheduled"
    tid2, wake_at = _park_with_wait(monkeypatch, "wait then park")
    with kbc.connect_closing() as conn:
        kb.unblock_task(conn, tid2)
        kb.claim_task(conn, tid2)
        kb.schedule_task(conn, tid2, reason="calendar release window", clock_only=True,
                         awaiting={"kind": "clock", "why": "the calendar release window"})
        kb.add_comment(conn, tid2, "t_other", "FYI")
        # The old continuation wake no longer applies; only the new park's own (default) wake does.
        assert kc.wake_due_continuations(conn, now=wake_at + 60) == []
        assert kb.get_task(conn, tid2).status == "scheduled"


def test_continue_refuses_stale_run_and_bad_args(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="default", max_runtime_seconds=600)
    run_id = _start_run(monkeypatch, tid, started_ago=10)
    assert "error" in json.loads(kt._handle_continue({"checkpoint": " "}))
    assert "error" in json.loads(kt._handle_continue({"checkpoint": "c", "resume_after_seconds": 10 ** 9}))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 99))
    assert "error" in json.loads(kt._handle_continue({"checkpoint": "c"}))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"


def test_background_notify_note_points_kanban_workers_at_wait_handoff(monkeypatch):
    from types import SimpleNamespace
    import gateway.session_context as sc
    from tools import terminal_tool_background as ttb

    monkeypatch.setattr(sc, "async_delivery_supported", lambda: False)
    for task, expect in ((None, False), ("t_x", True)):
        if task:
            monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        else:
            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        data: dict = {}
        assert ttb._apply_async_support(SimpleNamespace(id="p"), data, True, None) == (False, None)
        assert ("resume_after_seconds" in data["notify_unsupported"]) is expect
