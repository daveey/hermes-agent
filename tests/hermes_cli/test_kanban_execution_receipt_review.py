"""Review findings F1-F4 of the completion-receipt observation milestone, driven by real producers."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_operator
from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_session_activity as activity
from hermes_cli.execution_observations import project
from hermes_cli.kanban_completion_inputs import read_completions, read_verdicts
from hermes_cli.kanban_completion_receipt_projection import read_completed, read_completed_many
from hermes_cli.kanban_execution_receipt import done_raw
from hermes_cli.kanban_goal_receipt import GoalVerification
from tests.hermes_cli import test_kanban_execution_receipt as base
from tests.hermes_cli.test_kanban_execution_receipt import (
    GATEWAY, annotate, card, done_listing, finish, observation, owner_verdict)
from tests.hermes_state.test_completion_receipt import receipt

board = base.board


def goal_finish(conn, *, body="small body", handoff="Finished"):
    tid = kb.create_task(conn, title="Goal", body=body, assignee="coder", session_id="session",
                         completion_contract="local-only", goal_mode=True)
    assert kb.claim_task(conn, tid) is not None
    proof = GoalVerification.for_handoff(kb.get_task(conn, tid), handoff)
    assert kb.complete_task(conn, tid, summary=handoff, metadata={"completion_receipt": receipt("delivered")},
                            goal_verification=proof)
    return tid


@pytest.mark.parametrize("kwargs", [{}, {"body": "x" * 40000}, {"handoff": "Finished\n" + "y" * 40000}],
                         ids=["small", "large-body", "large-handoff"])
def test_f1_verified_goal_card_has_one_verdict_at_any_payload_size(board, kwargs):
    tid = goal_finish(board, **kwargs)
    assert owner_verdict(board, tid) == (True, False)
    assert read_verdicts(board, [tid])[tid] == (True, False)
    assert observation(done_listing(tid))["state"] == "done"


def test_f2_one_cards_malformed_evidence_does_not_blank_other_done_cards(board):
    healthy = finish(board, card(board))
    bad = goal_finish(board)
    with board:
        ev = board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='completed'", (bad,)).fetchone()
        body = json.loads(ev[1])
        body["summary"] = 5
        board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(body), ev[0]))
    activity._reset_cache_for_tests()
    assert read_verdicts(board, [healthy, bad]) == {healthy: (True, False), bad: (False, False)}
    states = {t: [v["state"] for v in r["execution_observations"]] for t, r in done_listing().items()}
    assert states[healthy] == ["done"] and states[bad] == ["unknown"]
    done = [v for v in annotate().get("execution_observations", []) if v["state"] == "done"]
    assert [v["identity"]["work_id"] for v in done] == [healthy]


def test_f3_clock_step_back_between_completions_still_observes_done(board, monkeypatch):
    tid = finish(board, card(board))
    assert kanban_db_operator.reopen_done_task(board, tid, reason="redo", actor="operator")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3600)
    finish(board, tid)
    monkeypatch.setattr(time, "time", real)
    activity._reset_cache_for_tests()
    latest = board.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0]
    assert read_completed(board, tid, gateway=GATEWAY, board="default")["completion_receipt_source"]["event_id"] == latest
    assert read_completed_many(board, [tid], board="default", gateway=GATEWAY)[tid][
        "completion_receipt_source"]["event_id"] == latest
    assert read_verdicts(board, [tid])[tid] == owner_verdict(board, tid) == (True, False)
    assert observation(done_listing(tid))["state"] == "done"


def test_f4_stale_replay_of_a_done_row_is_unknown_and_keeps_the_receipt(board):
    tid = finish(board, card(board))
    now = time.time()
    raw = done_raw(tid, read_completions(board, [tid])[tid],
                   read_completed_many(board, [tid], board="default", gateway=GATEWAY)[tid],
                   board="default", gateway=GATEWAY, profile="default", observed_at=now)
    current = observations.card({"id": tid, "status": "done", "_execution_done": raw}, now=now + 10)
    assert current["state"] == "done"
    stale = observations.card({"id": tid, "status": "done", "stale": True, "_execution_done": raw}, now=now + 10)
    assert stale["state"] == "unknown" and stale["unknown_reason"] == "source_unavailable"
    assert stale["last_known_state"] == "done"
    assert stale["delivery_receipt"] == current["delivery_receipt"]
    assert project(stale, now=now + 10)["state"] == "unknown"
