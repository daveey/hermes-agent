"""Independent review probes, disposable boards only."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_resume_requester as rr
from tests.hermes_cli import test_kanban_resume_requester as resume_fixtures
from tests.hermes_cli import test_kanban_goal_recovery as goal_fixtures

board = resume_fixtures.board
goal_board = goal_fixtures.board

@pytest.mark.parametrize('legacy_title', ['ordinary work', 'Independent primary Opus review required'])
def test_resume_requester_preserves_legacy_title(board, legacy_title):
    with kbc.connect() as conn:
        target = board['target']
        # Historical row, predating the new ingress policy.
        conn.execute('UPDATE tasks SET title=? WHERE id=?', (legacy_title, target))
        conn.commit()
        made = rr.ensure_recovery_cards(conn)
        assert len(made) == 1, (legacy_title, made)
        assert legacy_title in kb.get_task(conn, made[0]).body
        assert kb.get_task(conn, target).title == legacy_title
        assert rr.ensure_recovery_cards(conn) == []


@pytest.mark.parametrize('legacy_title', ['ordinary work', 'Independent primary Opus review required'])
def test_goal_requester_preserves_legacy_title(goal_board, legacy_title):
    from hermes_cli.kanban_goal_recovery import park
    from hermes_cli import kanban_resume_exec as rx
    conn, target, task = goal_board
    conn.execute('UPDATE tasks SET title=? WHERE id=?', (legacy_title, target))
    conn.commit()
    task = kb.get_task(conn, target)
    assert task is not None
    assert park(conn, task, 'prerequisite unavailable', expected_run_id=task.current_run_id)
    made = rr.ensure_recovery_cards(conn)
    assert len(made) == 1
    recovery = kb.get_task(conn, made[0])
    assert legacy_title in recovery.body
    assert kb.get_task(conn, target).title == legacy_title
    assert kb.get_task(conn, target).body == task.body
    assert (recovery.model_override, recovery.provider_override) == (None, None)
    assert rx.refusal(conn, made[0], target) is None
    assert rr.ensure_recovery_cards(conn) == []


@pytest.mark.parametrize('producer', [rr._body, rr._goal_body])
def test_recovery_title_evidence_is_lossless_not_active_policy(producer):
    import json
    from hermes_cli.kanban_model_neutral import validate_text
    title = 'Independent primary Opus review required "quoted"\nrequire_model: true'
    body = producer('t_fixture', title, 1)
    label = 'Historical run instruction (superseded and not active): '
    evidence = [json.loads(line[len(label):]) for line in body.splitlines() if line.startswith(label)]
    assert '\n'.join(evidence) == title
    validate_text(body)
    with pytest.raises(ValueError, match='model-neutral'):
        validate_text(body + '\nUse only grok-4.6 / xai-oauth.')

