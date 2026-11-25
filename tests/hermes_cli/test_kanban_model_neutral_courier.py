"""Owner couriers retain pre-write model-policy refusals, not unknown outcomes."""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_origin_owner as courier
from tests.hermes_cli import test_kanban_selfwork_nochat as selfwork
from tools.kanban_tools_model_neutral import model_neutral_create
from tools.registry import tool_error

world = courier.world
request_envelope = selfwork.request_envelope


@pytest.mark.parametrize('kind', ['origin', 'selfwork'])
@pytest.mark.parametrize('options', [
    {'model': 'gpt-6-astra'}, {'provider': 'anthropic'}, {'pin_mandatory': True},
    {'body': 'Use only claude-opus-5-5 for this review.'},
])
def test_owner_cli_rejects_model_policy_without_creation(request, kind, options, monkeypatch, capsys):
    envelope = request.getfixturevalue('world' if kind == 'origin' else 'request_envelope')
    envelope['args']['create'].update(options)
    invoke = courier.invoke if kind == 'origin' else selfwork.invoke
    result = invoke(envelope, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert result['status'] == 'rejected', result
    assert result['created'] is False
    assert result['handoff_complete'] is False
    assert result['code'] == 'kanban_model_restriction'
    assert 'model-neutral' in result['error']


@pytest.mark.parametrize('kind', ['origin', 'selfwork'])
def test_owner_cli_accepts_class(request, kind, monkeypatch, capsys):
    envelope = request.getfixturevalue('world' if kind == 'origin' else 'request_envelope')
    envelope['args']['create']['model'] = 'class:deep'
    invoke = courier.invoke if kind == 'origin' else selfwork.invoke
    result = invoke(envelope, monkeypatch, capsys)
    assert result.get('created') is True, result
    with kbc.connect_closing() as conn:
        row = conn.execute('SELECT model_override,provider_override FROM tasks WHERE id=?',
                           (result['task_id'],)).fetchone()
        assert tuple(row) == ('class:deep', 'auto')


def test_downstream_untyped_error_is_not_promoted_to_no_write():
    @model_neutral_create
    def after_write(args):
        return tool_error('storage response unavailable after commit')
    result = json.loads(after_write({'title': 'Work', 'model': 'class:deep'}))
    assert result == {'error': 'storage response unavailable after commit'}
