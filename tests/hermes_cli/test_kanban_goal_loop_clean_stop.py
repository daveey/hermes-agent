"""A goal-mode worker whose loop stops on a dispatcher-owned status exits clean; a broken loop does not. [daveey]

t_82f0fe15 (2026-10-30): a goal-mode worker that parks itself (``kanban_schedule``) ends its run as
``scheduled``; the run's own view (``goal_run_status``) then reads ``scheduled`` and the loop stops.
That stop must exit 0 so the dispatcher keeps the ``scheduled`` outcome and counts no failure. A loop
that broke (a follow-up turn raised, the judge or the status read failed) must still exit non-zero
instead of looking like a clean exit on the last good turn's result.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.turn_exit import turn_exit_code

OK_TURN = {"final_response": "parked until the PR merges", "completed": True}
# Every lifecycle status the dispatcher (or an operator) owns once the worker's run is no longer live.
DISPATCHER_OWNED = ["scheduled", "superseded", "continued", "blocked", "archived", "triage", "todo",
                    "review", "done", "changes_requested", None]


def _judge(monkeypatch, verdict="continue", transport_failed=False):
    def fake(goal, response, subgoals=None, background_processes=None, **_kw):
        return verdict, f"scripted:{verdict}", False, None, transport_failed
    monkeypatch.setattr(goals, "judge_goal", fake)


def _exit(result, loop):
    return turn_exit_code(goals.kanban_goal_loop_turn_result(result, loop), kanban_worker=True)


@pytest.mark.parametrize("status", DISPATCHER_OWNED)
def test_dispatcher_owned_status_is_a_clean_stop_exit_0(monkeypatch, status):
    _judge(monkeypatch)
    turns = []
    loop = goals.run_kanban_goal_loop(
        task_id="t1", goal_text="g", run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: status, block_fn=lambda r: pytest.fail("must not block"),
        first_response="parked")
    assert loop["failed"] is False
    assert turns == []
    assert _exit(OK_TURN, loop) == 0


def test_run_turn_exception_still_exits_nonzero(monkeypatch):
    _judge(monkeypatch)

    def boom(_prompt):
        raise RuntimeError("model call died")
    loop = goals.run_kanban_goal_loop(task_id="t1", goal_text="g", run_turn=boom,
                                      task_status_fn=lambda: "running", block_fn=lambda r: None,
                                      first_response="first")
    assert loop["outcome"] == "stopped" and loop["reason"] == "run_turn error: RuntimeError"
    assert loop["failed"] is True
    assert _exit(OK_TURN, loop) == 1


def test_judge_transport_failure_still_exits_nonzero(monkeypatch):
    _judge(monkeypatch, verdict="wait", transport_failed=True)
    loop = goals.run_kanban_goal_loop(task_id="t1", goal_text="g", run_turn=lambda p: "x",
                                      task_status_fn=lambda: "running", block_fn=lambda r: None)
    assert loop["failed"] is True
    assert _exit(OK_TURN, loop) == 1


def test_status_read_failure_still_exits_nonzero(monkeypatch):
    _judge(monkeypatch)

    def unreadable():
        raise OSError("board locked")
    loop = goals.run_kanban_goal_loop(task_id="t1", goal_text="g", run_turn=lambda p: "x",
                                      task_status_fn=unreadable, block_fn=lambda r: None)
    assert loop["failed"] is True
    assert _exit(OK_TURN, loop) == 1


def test_failed_follow_up_turn_keeps_its_provider_exit(monkeypatch):
    """A follow-up turn that failed on a rate-limit wall keeps EX_TEMPFAIL (requeued uncounted)."""
    _judge(monkeypatch)
    failed = {"final_response": "", "failed": True, "failure_reason": "rate_limit"}
    loop = goals.run_kanban_goal_loop(
        task_id="t1", goal_text="g", run_turn=lambda p: {"response": "", "failed": True, "failure_reason": "rate_limit"},
        task_status_fn=lambda: "running", block_fn=lambda r: None)
    assert loop["failed"] is True
    assert _exit(failed, loop) == kb.KANBAN_RATE_LIMIT_EXIT_CODE


def test_no_loop_keeps_the_turn_result():
    assert goals.kanban_goal_loop_turn_result(OK_TURN, None) is OK_TURN


# ---------------------------------------------------------------------------
# Managed worker path (gateway.session_kanban._run_task_turns): the production entry point.
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _running_goal_card(conn):
    tid = kb.create_task(conn, title="goal card", body="merge the PR", assignee="default", goal_mode=True)
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w4242")
    return tid, kb.get_task(conn, tid).current_run_id


class _Agent:
    session_id = "s1"

    def __init__(self, on_turn):
        self.calls, self._on_turn = 0, on_turn
        self._session_db = type("DB", (), {"get_messages_as_conversation": lambda _s, _sid: []})()

    def run_conversation(self, prompt, conversation_history=None, turn_author=None):
        self.calls += 1
        return self._on_turn(self.calls)


def _managed(monkeypatch, tid, run_id, agent):
    from agent import kanban_turn_recovery as recovery
    from agent import skill_commands
    from gateway import session_kanban
    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", lambda *a, **k: ("", [], []))
    monkeypatch.setattr(recovery, "recover_failed_kanban_turns", lambda *a, **k: None)
    context = {"skills": [], "context": "", "task_id": tid, "run_id": run_id, "goal_mode": True,
               "goal_text": "goal card\n\nmerge the PR", "goal_max_turns": 5}
    result = session_kanban._run_task_turns(agent, {"text": "work kanban task"}, [], context)
    return turn_exit_code(result, kanban_worker=True)


def test_managed_worker_that_parks_itself_exits_0_and_keeps_scheduled(kanban_home, monkeypatch):
    _judge(monkeypatch)
    with kbc.connect() as conn:
        tid, run_id = _running_goal_card(conn)

        def turn(n):
            # The worker's first turn parks the card (kanban_schedule / kanban_block kind=time).
            with kbc.connect() as c:
                assert kb.schedule_task(c, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z PR not merged",
                                        expected_run_id=run_id, clock_only=True,
                                        awaiting={"kind": "clock", "why": "the PR merge window"})
            return dict(OK_TURN)
        agent = _Agent(turn)
        assert kb.goal_run_status(conn, tid, run_id) == "running"
        code = _managed(monkeypatch, tid, run_id, agent)
        assert agent.calls == 1  # no continuation turn after the park
        assert code == 0
        assert kb.goal_run_status(conn, tid, run_id) == "scheduled"  # the run's own park, not an ownership loss
        run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
        task = kb.get_task(conn, tid)
        assert run["outcome"] == "scheduled"
        assert task.status == "scheduled" and task.consecutive_failures == 0


def test_managed_worker_whose_follow_up_turn_raises_exits_nonzero(kanban_home, monkeypatch):
    _judge(monkeypatch)
    with kbc.connect() as conn:
        tid, run_id = _running_goal_card(conn)

    def turn(n):
        if n == 1:
            return dict(OK_TURN)
        raise RuntimeError("provider socket closed")
    agent = _Agent(turn)
    assert _managed(monkeypatch, tid, run_id, agent) == 1
    assert agent.calls == 2


# ---------------------------------------------------------------------------
# Dispatcher booking of a parked goal worker that exits 0.
# ---------------------------------------------------------------------------


def test_parked_goal_worker_exit_0_is_not_booked_as_a_crash(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
    with kbc.connect() as conn:
        tid, run_id = _running_goal_card(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=4242 WHERE id=?", (tid,))
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait",
                                expected_run_id=run_id, clock_only=True, awaiting={"kind": "clock", "why": "fixture wait"})
        kbd.detect_crashed_workers(conn)
        run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
        task = kb.get_task(conn, tid)
        assert run["outcome"] == "scheduled"
        assert task.status == "scheduled" and task.consecutive_failures == 0
