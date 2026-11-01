"""A model-edit authority gap must not masquerade as missing supervisor capacity."""
import pytest

from hermes_cli import kanban_assignment_routing as ar
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_assignment_routing as routing_tests

board = routing_tests.board

OTHER = 't_0123456789abcdef'


@pytest.mark.parametrize('reason', [
    f'Change {OTHER} model pin to an available subscription route',
    f'Set model on {OTHER} to gpt-6-astra',
    f'Clear the provider override on task {OTHER}',
    f'Edit {OTHER}\'s unavailable Opus/Anthropic pin',
    f'Need non-worker card-edit authority to change {OTHER}\'s unavailable Opus/Anthropic pin to an available subscription route',
    f'Use kanban set-model {OTHER}',
    f'{OTHER} needs a model change',
])
def test_model_edit_is_authority_need(reason):
    assert ar.execution_authority_need('t_ffffffffffffffff', reason) == [OTHER]


@pytest.mark.parametrize('reason', [
    f'The model on {OTHER} was changed yesterday; wrong worker',
    f'Changed model pin on {OTHER}; worker lacks terminal tools',
    f'Inspect {OTHER} model and provider without changing them',
    f'Need the model output that {OTHER} produced',
])
def test_model_history_is_not_authority_need(reason):
    assert ar.execution_authority_need('t_ffffffffffffffff', reason) == []


def test_own_model_edit_not_other_card():
    assert ar.execution_authority_need(OTHER, f'Change {OTHER} model pin') == []


@pytest.mark.parametrize('candidate', ['eligible', 'absent', 'ineligible'])
def test_model_authority_refused_before_candidate_selection(board, candidate):
    conn, home = board
    if candidate == 'absent':
        (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: missing\n')
    elif candidate == 'ineligible':
        (home / 'profiles/supervisor/config.yaml').write_text(
            'agent:\n  disabled_toolsets: [kanban]\n')
    tid = kb.create_task(conn, title='Recover owner', assignee='worker', created_by='worker')
    before = kb.get_task(conn, tid)
    with pytest.raises(ar.ExecutionAuthorityMismatch) as exc:
        kb.block_task(conn, tid, kind='assignment',
                      reason=f'Change {OTHER} model pin to an available subscription route')
    message = str(exc.value)
    assert 'model' in message and 'non-worker' in message
    assert 'ops=[{"op": "resume"' not in message
    after = kb.get_task(conn, tid)
    assert before is not None and after is not None
    assert (after.status, after.assignee, after.model_override, after.provider_override) == (
        before.status, before.assignee, before.model_override, before.provider_override)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert not conn.execute('SELECT 1 FROM task_links').fetchone()


def test_legacy_model_edit_repair_cannot_release_original(board, monkeypatch):
    import json

    conn, _ = board
    tid = kb.create_task(conn, title='Recover owner', assignee='worker', created_by='creator')
    with monkeypatch.context() as m:
        m.setattr(ar, 'execution_authority_need', lambda *a: [])
        assert kb.block_task(conn, tid, kind='assignment', reason=f'Change {OTHER} model pin')
    raw = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC",
        (tid,),
    ).fetchone()[0]
    repair_id = json.loads(raw)['supervisor_task_id']
    with pytest.raises(ar.ExecutionAuthorityMismatch, match='model/provider'):
        kb.complete_task(conn, repair_id, result='Profile exists')
    assert kb.get_task(conn, tid).status == 'todo'
    assert kb.get_task(conn, repair_id).status != 'done'


def test_tool_model_edit_refusal_is_typed(board, monkeypatch):
    import json

    from tools import kanban_tools as tools
    from tools.registry import registry

    conn, _ = board
    tid = kb.create_task(conn, title='Recover owner', assignee='worker', created_by='creator')
    monkeypatch.setattr(tools, '_worker_guard', lambda *args: tid)
    monkeypatch.setattr(tools, '_worker_run_id', lambda *args: None)
    result = json.loads(registry.dispatch('kanban_block', {
        'task_id': tid, 'kind': 'assignment', 'reason': f'Change {OTHER} model pin',
    }))
    assert result['code'] == 'execution_authority_mismatch'
    assert kb.get_task(conn, tid).status == 'ready'
