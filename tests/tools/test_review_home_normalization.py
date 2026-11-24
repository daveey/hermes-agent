import json
import pytest
from hermes_cli import kanban_creation_home as creation
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as nodes
from tests.tools import test_kanban_worker_canonical_home as home
from tools import kanban_tools as tools
svc, gws, pool_labels = home.svc, home.gws, home.pool_labels

@pytest.mark.parametrize('forwarded', [False, True])
@pytest.mark.parametrize('value', ['pool:worker', '["pool:worker"]'])
def test_runs_on_normalization(gws, monkeypatch, forwarded, value):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    if not forwarded:
        monkeypatch.setattr(creation, 'plan', lambda *a, **kw: None)
    sent = []
    def run(argv):
        if argv[-1] == '--help':
            return 0, '--option canonical-home-v1', ''
        sent.append(argv)
        return 3, 'queued', ''
    monkeypatch.setattr(placement, 'hub_runner', run)
    monkeypatch.setattr(placement, 'remote_state', lambda _: None)
    placement._help_cache.clear()
    result = json.loads(tools._handle_create({'title': 'Normalization parity', 'assignee': 'default', 'requires': [], 'runs_on': value}))
    if not forwarded:
        assert result.get('task_id'), result
        actual = json.loads(worker.conn.execute('SELECT runs_on FROM tasks WHERE id=?', (result['task_id'],)).fetchone()[0])
    else:
        assert result.get('placed', {}).get('gateway') == 'hub', result
        argv = sent[0]
        options = {k: json.loads(v) for k,v in (a.split('=',1) for i,a in enumerate(argv) if i and argv[i-1]=='--option')}
        actual = options['runs_on']
    assert actual == wn.normalize_runs_on(value)
