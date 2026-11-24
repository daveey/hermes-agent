import json
import pytest
from hermes_cli import kanban_placement as placement
from tests.tools import test_kanban_worker_canonical_home as home
from tests.hermes_cli import test_kanban_worker_node as nodes
from tools import kanban_tools as tools
svc, gws, pool_labels = home.svc, home.gws, home.pool_labels

@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_any_machine_preserved(gws, monkeypatch, legacy):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {nodes.WORKER: 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    sent = []
    def send(view, **kwargs):
        sent.append(view)
        return {'ok': True, 'request_id': 'empty-proof', 'queued_only': True}
    monkeypatch.setattr(placement, 'create_remote', send)
    if legacy:
        from hermes_cli import kanban_creation_home
        monkeypatch.setattr(kanban_creation_home, 'plan', lambda *a, **kw: None)
    result = json.loads(tools._handle_create({'title': 'Any machine permitted', 'assignee': 'default', 'requires': [], 'runs_on': []}))
    if legacy:
        row = worker.conn.execute('SELECT runs_on FROM tasks WHERE id=?', (result['task_id'],)).fetchone()
        assert json.loads(row[0]) == []
    else:
        assert result.get('placed', {}).get('gateway') == 'hub', result
        assert sent[0]['runs_on'] == []
