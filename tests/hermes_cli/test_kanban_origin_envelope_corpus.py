"""Production envelope compatibility at authenticated owner dispatch planning.

Corpus values are redacted, not invented. Route bindings are rebound to the
isolated owner fixture; execution/authorization lookups remain fixture-owned.
This proves validator/owner dispatch-plan parity, not historical job execution.
"""
import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_creation_contract as contract
from hermes_cli import kanban_origin_owner as owner
from tests.hermes_cli import test_kanban_origin_owner as support

world = support.world
CORPUS = json.loads((Path(__file__).parents[1] / 'fixtures' / 'kanban_origin_envelopes.json').read_text())


def test_production_corpus_inventory():
    samples = CORPUS['samples']
    assert len(samples) >= 30
    assert len({row['source_sha256'] for row in samples}) == len(samples)
    assert {row['envelope']['action'] for row in samples} == {'origin-forward', 'selfwork-create'}
    assert any(row['envelope']['args']['create'].get('placement') == 'auto' for row in samples)
    assert any('tenant' in row['envelope']['args']['create'] and row['envelope']['args']['create']['tenant'] is None for row in samples)


@pytest.mark.parametrize('sample', CORPUS['samples'], ids=lambda row: str(row['sample']))
def test_production_owner_plan_matches_base(world, monkeypatch, sample):
    create = copy.deepcopy(sample['envelope']['args']['create'])
    create.setdefault('session_id', 'creator')
    # Both production routes eventually use common origin-owner dispatch when
    # chat-bound. Freeze unrelated route/auth fixture evidence, not create data.
    request = {**world, 'args': {**world['args'], 'create': create}}
    expected = {'tool': 'kanban_create', 'args': {**create, 'board': 'default'}}
    def capture(name, args, intent, selfwork_context=None, selfwork_exception=None):
        assert selfwork_exception is None
        assert intent['origin'] == world['args']['origin']
        return {'tool': name, 'args': args}
    monkeypatch.setattr(owner, '_dispatch_owner_tool', capture)
    assert owner.receive(request) == expected


@pytest.mark.parametrize('key', sorted(set(contract.properties()) - {'title'}))
def test_every_optional_field_accepts_null_at_owner(key):
    contract.validate_received({'title': 'Required title', key: None})


@pytest.mark.parametrize('bad', [{'title': None}, {'title': 'Valid', 'private_authority': None},
                                 {'title': 'Valid', 'priority': True}])
def test_null_compatibility_never_admits_required_unknown_or_wrong_type(bad):
    with pytest.raises(ValueError, match='origin_forward_create_invalid'):
        contract.validate_received(bad)


@pytest.mark.parametrize('key', sorted(set(contract.EXCLUDED) - {'session_id'}))
def test_receiver_compatibility_does_not_widen_canonical_transport(key):
    with pytest.raises(ValueError, match='origin_forward_create_invalid'):
        contract.validate_forward({'title': 'Valid', key: None})


def test_schema_explicit_null_value_is_preserved(monkeypatch):
    props = {**contract.properties(), 'future_nullable': {'type': ['string', 'null']}}
    monkeypatch.setattr(contract, 'properties', lambda: props)
    args = {'title': 'Valid', 'future_nullable': None, 'tenant': None}
    assert contract.payload(args) == {'title': 'Valid', 'future_nullable': None}
    contract.validate_forward(args)
    contract.validate_received(args)

