"""Generic triage entry points cannot bypass a judged goal's recovery budget."""
from unittest.mock import Mock

from hermes_cli import kanban_db as kb, kanban_specify as spec
from hermes_cli.kanban_goal_recovery import park
from tests.hermes_cli.test_kanban_goal_recovery import board as board  # noqa: PLC0414 -- pytest fixture re-export


def test_individual_specify_refuses_parked_goal_before_aux(board, monkeypatch):
    conn, tid, task = board
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    before = kb.get_task(conn, tid)
    events = kb.list_events(conn, tid)
    aux = Mock(return_value=('{"title":"Replacement", "body":"Weaker acceptance"}', ''))
    monkeypatch.setattr(spec, '_call_aux', aux)

    outcome = spec.specify_task(tid, author='operator')

    assert not outcome.ok
    assert 'goal_unachievable' in outcome.reason
    aux.assert_not_called()
    assert kb.get_task(conn, tid) == before
    assert kb.list_events(conn, tid) == events


def test_specify_all_selection_excludes_parked_goal(board):
    conn, _tid, task = board
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    fresh = kb.create_task(conn, title='Rough idea', triage=True)

    assert spec.list_triage_ids() == [fresh]


def test_auto_decompose_selection_excludes_previously_run_goal(board):
    from hermes_cli import kanban_decompose as decomp

    conn, tid, task = board
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    fresh = kb.create_task(conn, title='New goal', triage=True, goal_mode=True)

    assert 'already run' in (kb.decomposition_refusal(conn, tid) or '')
    assert decomp.list_triage_ids(skip_awaiting_human=True) == [fresh]
    parked = kb.get_task(conn, tid)
    assert parked is not None
    assert parked.body == task.body
