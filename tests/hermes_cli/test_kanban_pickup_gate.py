"""Pure pickup facts and real caller short-circuit/observation parity. [daveey]"""
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_pickup_gate as gate
from hermes_cli import kanban_worker_node as node


FACTS = dict(already_local=False, default_placement=False, review_disabled=False,
             placement_refusal=None, missing_profile=None, capability_refusal=None, source_refusal=None)


@pytest.mark.parametrize('name,value,reason', [
    ('already_local', True, ('', None)),
    ('default_placement', True, ('', None)),
    ('review_disabled', True, ('other', 'review dispatch disabled')),
    ('placement_refusal', 'held', ('hold', 'held')),
    ('placement_refusal', '', ('capability_missing', '')),
    ('missing_profile', "no profile 'other' here", ('profile_ineligible', "no profile 'other' here")),
    ('capability_refusal', '', ('capability_missing', '')),
    ('source_refusal', '', ('capability_missing', '')),
])
def test_rejections(name, value, reason):
    facts = dict(FACTS)
    facts[name] = value
    decision = gate.evaluate(facts)
    assert decision.complete and decision.reason == reason and decision.next_fact is None


def test_missing_is_not_null_and_hypothetical_requires_new_facts():
    facts = {'already_local': True}
    assert gate.evaluate(facts).complete
    facts['already_local'] = False
    decision = gate.evaluate(facts)
    assert not decision.complete and decision.next_fact == 'default_placement'
    assert gate.evaluate(FACTS).complete and gate.evaluate(FACTS).reason is None
    for name in FACTS:
        shortened = dict(FACTS)
        del shortened[name]
        assert gate.evaluate(shortened).next_fact == name
    with pytest.raises(FrozenInstanceError):
        decision.complete = True


@pytest.mark.parametrize('name,value', [('already_local', 1), ('default_placement', None),
                                        ('placement_refusal', False), ('source_refusal', [])])
def test_invalid_facts(name, value):
    with pytest.raises(ValueError):
        gate.evaluate(dict(FACTS, **{name: value}))


def test_observation_limits_and_nested_scope():
    with gate.collect(max_records=1, max_bytes=4096) as outer:
        gate.record('one', FACTS)
        with gate.collect(max_records=2, max_bytes=4096) as inner:
            gate.record('two', {'already_local': True})
        gate.record('three', FACTS)
        gate.record('four', FACTS)
    assert len(outer.records) == 1 and outer.gap == 'record_limit'
    assert inner.records[0].task_id == 'two' and inner.gap is None
    assert outer.records[0].complete
    with gate.collect(max_records=5, max_bytes=1) as tiny:
        gate.record('one', FACTS)
    assert not tiny.records and tiny.gap == 'byte_limit'
    gate.record('outside', FACTS)
    assert len(outer.records) == 1


def test_snapshot_copies_facts_and_records_unknown():
    facts = {'already_local': False}
    with gate.collect() as capture:
        gate.record('x', facts)
        facts['already_local'] = True
    snapshot = capture.records[0]
    assert snapshot.facts == (('already_local', False),)
    assert not snapshot.complete and snapshot.next_fact == 'default_placement'
    with pytest.raises(FrozenInstanceError):
        snapshot.task_id = 'other'


def test_collector_bad_payload_is_gap_not_exception():
    with gate.collect() as capture:
        gate.record('x', {'already_local': object()})
    assert capture.gap == 'invalid_facts' and not capture.records


def caller(monkeypatch, *, local=False, mode='pool', review_enabled=True, card_changes=None):
    from hermes_cli import kanban_runs_on_capabilities as capabilities
    from hermes_cli import kanban_workspace_source as source
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = (1,) if local else None
    placement = Mock(return_value=None)
    capability = Mock(return_value=None)
    workspace = Mock(return_value=None)
    profile = Mock(return_value=True)
    monkeypatch.setattr(node, 'eligible', placement)
    monkeypatch.setattr(capabilities, 'claim_refusal', capability)
    monkeypatch.setattr(source, 'pickup_skip', workspace)
    card = dict(id='task', status='ready', runs_on=[], assignee='default')
    card.update(card_changes or {})
    def invoke():
        return node._skip_reason(conn, card, 'worker', {}, mode=mode,
                                 review_enabled=review_enabled, profile_exists=profile)
    return invoke, placement, capability, workspace, profile


def test_real_caller_captures_complete_facts(monkeypatch):
    invoke, placement, capability, workspace, profile = caller(monkeypatch)
    with gate.collect() as capture:
        assert invoke() is None
    assert len(capture.records) == 1
    assert dict(capture.records[0].facts) == FACTS
    for reader in (placement, capability, workspace, profile):
        reader.assert_called_once()


def test_real_caller_preserves_short_circuit(monkeypatch):
    invoke, placement, capability, workspace, profile = caller(monkeypatch, local=True)
    with gate.collect() as capture:
        assert invoke() == ('', None)
    assert capture.records[0].facts == (('already_local', True),)
    for reader in (placement, capability, workspace, profile):
        reader.assert_not_called()


def test_real_caller_exception_partial_and_overflow(monkeypatch):
    invoke, placement, capability, workspace, profile = caller(monkeypatch)
    placement.side_effect = RuntimeError('reader failure')
    with gate.collect() as capture:
        with pytest.raises(RuntimeError, match='reader failure'):
            invoke()
    assert not capture.records[0].complete
    assert capture.records[0].next_fact == 'placement_refusal'
    capability.assert_not_called()
    workspace.assert_not_called()
    profile.assert_not_called()
    with gate.collect(max_records=0) as full:
        with pytest.raises(RuntimeError, match='reader failure'):
            invoke()
    assert full.gap == 'record_limit'


@pytest.mark.parametrize('options,reason', [
    ({'mode': 'explicit', 'card_changes': {'runs_on': None}}, ('', None)),
    ({'card_changes': {'status': 'review', 'runs_on': None}}, ('', None)),
    ({'review_enabled': False, 'card_changes': {'status': 'review'}},
     ('other', 'review dispatch disabled')),
])
def test_real_placement_and_review_short_circuits(monkeypatch, options, reason):
    invoke, placement, capability, workspace, profile = caller(monkeypatch, **options)
    with gate.collect() as capture:
        assert invoke() == reason
    assert capture.records[0].reason == reason
    for reader in (placement, capability, workspace, profile):
        reader.assert_not_called()


@pytest.mark.parametrize('value', ['held', '', 'labels_mismatch'])
def test_real_placement_refusal(monkeypatch, value):
    invoke, placement, capability, workspace, profile = caller(monkeypatch)
    placement.return_value = value
    with gate.collect() as capture:
        assert invoke() == ('hold' if value == 'held' else 'capability_missing', value)
    assert dict(capture.records[0].facts)['placement_refusal'] == value
    for reader in (capability, workspace, profile):
        reader.assert_not_called()


def test_real_missing_profile(monkeypatch):
    invoke, _placement, capability, workspace, profile = caller(monkeypatch)
    profile.return_value = False
    assert invoke() == ('profile_ineligible', "no profile 'default' here")
    capability.assert_not_called()
    workspace.assert_not_called()


def test_actual_surrogate_refusal_remains_refusal(monkeypatch):
    invoke, _placement, _capability, workspace, _profile = caller(monkeypatch)
    workspace.return_value = '\ud800'
    with gate.collect() as capture:
        assert invoke() == ('capability_missing', '\ud800')
    assert capture.gap == 'invalid_facts' and not capture.records
