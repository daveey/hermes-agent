"""Shared schema validation is the courier's read-only expand/contract gate."""
import io
import json

import pytest

from hermes_cli import kanban_creation_contract as contract
from hermes_cli import kanban_origin_forward_decision as forward


@pytest.mark.parametrize('args,valid', [
    ({'title': 'Portable', 'assignee': 'default', 'requires': [], 'human_action_now': True}, True),
    ({'title': 'Portable', 'assignee': 'default', 'review_required': False, 'priority': 0}, True),
    ({'title': 'Portable', 'assignee': 'default', 'human_action_now': 'true'}, False),
    ({'title': 'Portable', 'assignee': 'default', 'priority': True}, False),
    ({'title': 'Portable', 'assignee': 'default', 'new_unknown': 1}, False),
    ({'title': 'Portable', 'assignee': 'default', 'creator_task_id': 't_0123456789abcdef'}, False),
])
def test_read_only_preflight(monkeypatch, capsys, args, valid):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(args)))
    assert contract.main() == (0 if valid else 2)
    assert json.loads(capsys.readouterr().out)['valid'] is valid


@pytest.mark.parametrize('key', sorted(contract.EXCLUDED))
def test_local_controls_cannot_enter_generic_payload(monkeypatch, capsys, key):
    args = {'title': 'Portable', 'assignee': 'default', key: 'caller-controlled'}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(args)))
    assert contract.main() == 2
    assert json.loads(capsys.readouterr().out)['valid'] is False


def test_new_schema_field_forwards_without_transport_edit(monkeypatch):
    props = {**contract.properties(), 'new_public_field': {'type': 'object'}}
    monkeypatch.setattr(contract, 'properties', lambda: props)
    args = {'title': 'Portable', 'assignee': 'default', 'new_public_field': {'explicit': []}}
    assert contract.payload(args) == args
    forward._validate_create({**args, 'session_id': 'qualified-origin'})
    with pytest.raises(ValueError, match='origin_forward_create_invalid'):
        forward._validate_create({**args, 'new_public_field': 1, 'session_id': 'qualified-origin'})


def test_preflight_requires_complete_finite_bounded_json(monkeypatch, capsys):
    for text in ['{}', 'not-json', '"' + 'x' * 262145 + '"']:
        monkeypatch.setattr('sys.stdin', io.StringIO(text))
        assert contract.main() == 2
        assert json.loads(capsys.readouterr().out)['valid'] is False
