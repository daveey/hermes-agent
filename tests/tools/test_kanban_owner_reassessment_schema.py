"""The owner opt-in is a real strict schema, without weakening existing park policy."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from hermes_cli import kanban_owner_reassessment as r
from tests.hermes_cli.test_kanban_owner_reassessment import optin
from tools import kanban_tools as tools
from tools.registry import registry


@pytest.mark.parametrize('name', ['kanban_schedule', 'kanban_continue'])
def test_owner_reassessment_is_declared_not_a_compatibility_argument(name):
    schema = registry.get_schema(name)
    field = schema['parameters']['properties']['owner_reassessment']
    assert 'owner_reassessment' not in tools._UNDECLARED_ARGS.get(name, ())
    assert 'owner_reassessment' not in schema['parameters'].get('required', ())
    assert Draft202012Validator(field).is_valid(optin())
    assert field['additionalProperties'] is False
    assert set(field['required']) == {'version', 'reassess_at', 'target', 'evidence_refs'}
    assert field['properties']['target']['additionalProperties'] is False
    assert field['properties']['evidence_refs']['items']['additionalProperties'] is False
    desc = field['description']
    for claim in ('same-run', 'backstop', 'pending evidence', 'not producer health', 'privately'):
        assert claim in desc
    assert len(json.dumps(field).encode()) < 1900


@pytest.mark.parametrize('change', [
    lambda v: v.update(issuer={'session_id': 'forged'}),
    lambda v: v.update(version=True), lambda v: v.update(reassess_at=True),
    lambda v: v.update(reassess_at=0), lambda v: v['target'].update(revision=''),
    lambda v: v['target'].update(kind='clock'), lambda v: v['target'].update(identity=' '),
    lambda v: v['target'].update(identity='x' * 513), lambda v: v['target'].update(revision='x' * 257),
    lambda v: v['target'].update(authority=True), lambda v: v.update(evidence_refs=[]),
    lambda v: v['evidence_refs'].append(deepcopy(v['evidence_refs'][0])),
    lambda v: v['evidence_refs'][0].update(uid='legacy:1'),
    lambda v: v['evidence_refs'][0].update(uid=v['evidence_refs'][0]['uid'] + '\n'),
    lambda v: v['evidence_refs'][0].update(table='comments'),
    lambda v: v['evidence_refs'][0].update(issuer='forged'),
])
def test_served_schema_and_runtime_reject_same_invalid_optins(change):
    value = optin()
    change(value)
    for name in ('kanban_schedule', 'kanban_continue'):
        field = registry.get_schema(name)['parameters']['properties']['owner_reassessment']
        assert not Draft202012Validator(field).is_valid(value), name
    with pytest.raises(r.ReassessmentRefused):
        r.request(value)


def test_schema_addition_preserves_every_existing_policy_description_and_field():
    # Load the source definitions independently, before the registration module's additive mutation.
    source = Path(tools.__file__).with_name('kanban_tools_schemas.py')
    spec = importlib.util.spec_from_file_location('_unmodified_park_schema', source)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    for name, constant in [('kanban_schedule', 'KANBAN_SCHEDULE_SCHEMA'), ('kanban_continue', 'KANBAN_CONTINUE_SCHEMA')]:
        actual = deepcopy(registry.get_schema(name))
        del actual['parameters']['properties']['owner_reassessment']
        assert actual == getattr(original, constant)
