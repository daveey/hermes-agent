"""Canonical-home forwarding keeps constraints and authenticated origin custody."""
import copy
import json

import pytest

from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_origin_forward_decision as origins
from hermes_cli import kanban_creation_home as home

forwarding = origins.forwarding
authority = origins.authority


@pytest.mark.parametrize('cfg,gateway,label', [
    ({'worker_queue': {'hub': 'pool:worker'}}, 'worker-asg12', 'pool:worker'),
    ({'worker_queue': {'hub': 'pool:worker', 'worker-a3': 'pool:special'}}, 'worker-a3', 'pool:special'),
    ({}, 'worker-a3', None),
    ({'worker_queue': []}, 'worker-a3', None),
    ({'worker_queue': {'hub': 'machine:hub'}}, 'worker-a3', None),
    ({'worker_queue': {'hub': 'pool:worker'}}, 'personal-linux', None),
    ({'worker_queue': {'hub': 'pool:worker'}}, None, None),
    ({'worker_queue': {'hub': 'pool:worker'}}, 'worker', None),
])
def test_dynamic_worker_policy_keeps_opt_in_and_explicit_routes(cfg, gateway, label):
    before = copy.deepcopy(cfg)
    effective = home._worker_config(cfg, gateway)
    assert placement._Cfg(effective).queue_label(gateway) == label
    assert cfg == before



@pytest.mark.parametrize('labels', [['pool:worker'], ['machine:macbook']])
def test_foreign_origin_authorizes_complete_home_metadata(forwarding, labels):
    request = copy.deepcopy(forwarding[0])
    metadata = {'runs_on': labels, 'parents': ['t_0123456789abcdef'], 'tenant': 'hermes',
                'max_retries': 2, 'initial_status': 'blocked', 'human_request': {'action': 'retained ask'}}
    request['args']['create'].update(metadata)
    result = origins.decide(forwarding, request)
    assert all(result['args']['create'][k] == v for k, v in metadata.items())
    assert forwarding[2] == [('default', 'task-1')]
    assert origins.decide(forwarding, request, now_ms=lambda: 99999) == result
    changed = copy.deepcopy(request)
    changed['args']['create']['runs_on'] = ['machine:hub']
    with pytest.raises(ValueError, match='origin_forward_conflict'):
        origins.decide(forwarding, changed)


@pytest.mark.parametrize('available', [False, True])
def test_transport_preflights_actual_client_and_preserves_options(monkeypatch, available):
    argv_seen = []

    def run(argv):
        argv_seen.append(argv)
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2 canonical-home-v1' if available else '--option', ''
        return 0, 'queued', ''

    placement._help_cache.clear()
    monkeypatch.setattr(placement, 'hub_runner', run)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    args = {'title': 'portable proof', 'assignee': 'default', 'runs_on': ['pool:worker'],
            'parents': ['t_0123456789abcdef'], 'tenant': 'hermes', 'max_retries': 2,
            'initial_status': 'blocked', 'human_request': {'action': 'retained ask'}}
    result = placement.create_remote(args, target='hub', origin_session=None,
        origin_profile='default', self_gw='worker-1', cfg={}, hub_cli='fake-courier', canonical_home=True)
    sent = [argv for argv in argv_seen if argv[-1] != '--help']
    if not available:
        assert result['ok'] is False and result['fatal'] is True
        assert not sent
        return
    assert result['ok'] is True and len(sent) == 1
    options = dict(value.split('=', 1) for i, value in enumerate(sent[0]) if i and sent[0][i-1] == '--option')
    assert all(json.loads(options[key]) == args[key] for key in
               ('runs_on', 'parents', 'tenant', 'max_retries', 'initial_status', 'human_request'))


@pytest.mark.parametrize('policy', ['explicit_worker', 'hub_pool'])
@pytest.mark.parametrize('failure', ['unclaimable', 'exception'])
def test_worker_planning_failure_is_typed_not_local(monkeypatch, failure, policy):
    from hermes_cli import kanban_worker_placement as workers

    def queue(*args):
        if failure == 'exception':
            raise RuntimeError('registry unavailable')

    monkeypatch.setattr(workers, 'queue_decision', queue)
    cfg = {'worker_queue': {'hub' if policy == 'hub_pool' else 'worker-a3': 'pool:worker'}}
    home_plan, _, decision = home.prepare({'title': 'must not stay on worker'},
        cfg=cfg, gateway='worker-a3', machine_ok=lambda _: False)
    assert home_plan is not None
    assert decision['where'] == 'refuse'
    assert decision['pin'] == 'hub'
