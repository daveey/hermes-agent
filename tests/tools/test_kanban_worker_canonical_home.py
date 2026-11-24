"""Execution labels must not turn an ephemeral worker into a card's home."""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_placement as placement
from tools import kanban_tools as tools
from tests.hermes_cli import test_kanban_worker_node as nodes

svc = nodes.svc
gws = nodes.gws


@pytest.fixture(autouse=True)
def pool_labels(monkeypatch):
    machines = {key: dict(value) for key, value in nodes.MACHINES.items()}
    machines[nodes.WORKER]['labels'] = ['linux', 'pool:worker', 'capability:core-ship',
                                         'capability:tailnet-exec']
    machines['macbook']['pickup'] = 'explicit'
    monkeypatch.setattr(nodes, 'MACHINES', machines)


@pytest.mark.parametrize('options,expected', [
    ({}, ['pool:worker']),
    ({'skills': ['session-status-panel', 'inspecting-hermes-desktop-dom'],
      'body': 'Inspect the Desktop DOM'}, ['pool:worker', 'capability:tailnet-exec']),
    ({'runs_on': ['pool:worker', 'capability:core-ship']},
     ['pool:worker', 'capability:core-ship']),
    ({'placement': 'macbook', 'body': 'Read the existing packaged Desktop on macbook'},
     ['machine:macbook']),
])
def test_worker_tool_forwards_home_and_retains_execution(gws, monkeypatch, options, expected):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'linux_pool_gateway', lambda *a, **kw: True)
    monkeypatch.setattr(placement, 'config', lambda: {
        'worker_queue': {nodes.WORKER: 'pool:worker'}, 'route': {nodes.WORKER: nodes.HUB}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda labels: True)
    sent = []

    def send(view, **kwargs):
        sent.append((view, kwargs))
        return {'ok': True, 'request_id': 'canonical-home-test', 'queued_only': True}

    monkeypatch.setattr(placement, 'create_remote', send)
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    out = json.loads(tools._handle_create({'title': 'canonical card', 'assignee': 'default',
                                         'requires': [], **options}))
    assert len(sent) == 1, out
    view, context = sent[0]
    assert context['target'] == nodes.HUB
    assert view['runs_on'] == expected
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
    assert out['placed']['gateway'] == nodes.HUB


def test_canonical_home_failure_never_falls_back_local(gws, monkeypatch):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'linux_pool_gateway', lambda *a, **kw: True)
    monkeypatch.setattr(placement, 'config', lambda: {
        'worker_queue': {nodes.WORKER: 'pool:worker'}, 'route': {nodes.WORKER: nodes.HUB}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda labels: True)
    monkeypatch.setattr(placement, 'create_remote', lambda *a, **kw: {'ok': False, 'error': 'unsupported'})
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    out = json.loads(tools._handle_create({'title': 'canonical failed', 'assignee': 'default', 'requires': []}))
    assert out.get('code') == 'placement_unavailable', out
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize('card,expected', [
    ({'title': 'portable follow-up'}, ['pool:worker']),
    ({'title': 'Inspect Desktop DOM', 'skills': ['session-status-panel', 'inspecting-hermes-desktop-dom']},
     ['pool:worker', 'capability:tailnet-exec']),
    ({'title': 'Machine-only installed Desktop inspection', 'placement': 'macbook'}, ['machine:macbook']),
    ({'title': 'Rotate Keychain token'}, ['pool:worker', 'capability:tailnet-exec']),
    ({'title': 'Read browser state in Chrome'}, ['pool:worker']),
    ({'title': 'Inspect process on this host'}, ['machine:worker-1']),
    ({'title': 'Explicit creator execution', 'placement': 'local'}, ['machine:worker-1']),
    ({'title': 'Explicit creator execution by name', 'placement': 'worker-1'}, ['machine:worker-1']),
])
def test_disposable_hub_really_owns_worker_created_card(gws, monkeypatch, card, expected):
    hub, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: Path(os.environ['HERMES_HOME']).parent.name)
    monkeypatch.setattr(placement, 'linux_pool_gateway', lambda gateway, *a, **kw: gateway == nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {
        'worker_queue': {nodes.WORKER: 'pool:worker'}, 'route': {nodes.WORKER: nodes.HUB}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda labels: True)
    received = []

    def courier(view, **kwargs):
        assert kwargs['target'] == nodes.HUB
        hub.enter()
        try:
            result = json.loads(tools._handle_create(view))
            received.append(result)
            assert result.get('task_id'), result
        finally:
            worker.enter()
        return {'ok': True, 'request_id': 'composed-canonical-home-test', 'queued_only': True}

    monkeypatch.setattr(placement, 'create_remote', courier)
    out = json.loads(tools._handle_create({'assignee': 'default', 'requires': [], **card}))
    assert out.get('placed', {}).get('gateway') == nodes.HUB, out
    assert len(received) == 1
    tid = received[0]['task_id']
    hub.round()  # sync stamps administrative ownership before publishing to Board
    stored = dict(hub.conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert stored['created_on'] == nodes.HUB
    assert sorted(json.loads(stored['runs_on'])) == sorted(expected)
    assert worker.conn.execute('SELECT 1 FROM tasks WHERE id=?', (tid,)).fetchone() is None


def test_worker_artifact_keeps_execution_not_storage(gws, monkeypatch):
    _hub, worker = gws
    worker.enter()
    monkeypatch.setattr(Path, 'home', lambda: Path(os.environ['HERMES_HOME']).parent)
    artifact = Path.home() / '.hermes' / 'cache' / 'scratch' / 'owned' / 'proof.txt'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('preserved source')
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: Path(os.environ['HERMES_HOME']).parent.name)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {nodes.WORKER: 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda labels: True)
    sent = []

    def courier(view, **kwargs):
        sent.append((view, kwargs))
        return {'ok': True, 'request_id': 'artifact-home-test', 'queued_only': True}

    monkeypatch.setattr(placement, 'create_remote', courier)
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    out = json.loads(tools._handle_create({'title': 'Read preserved source',
                                         'body': 'Read ~/.hermes/cache/scratch/owned/proof.txt',
                                         'assignee': 'default', 'requires': []}))
    assert out.get('placed', {}).get('gateway') == nodes.HUB, out
    assert sent[0][0]['runs_on'] == ['machine:' + nodes.WORKER]
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
    assert artifact.read_text() == 'preserved source'

