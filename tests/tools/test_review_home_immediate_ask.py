import json
import os
import pytest
from hermes_cli import kanban_db as kb, kanban_placement as placement, kanban_creation_home as creation
from tests.tools import test_kanban_worker_canonical_home as home
from tests.tools.test_kanban_home_landing_contract import REQUEST
from tools import kanban_tools as tools
svc, gws, pool_labels = home.svc, home.gws, home.pool_labels

@pytest.mark.parametrize('forwarded,diagnostic_restore', [(False, False), (True, False), (True, True)])
def test_immediate_ask_with_open_parent(gws, monkeypatch, forwarded, diagnostic_restore):
    hub, worker = gws
    owner = hub if forwarded else worker
    owner.enter()
    parent = kb.create_task(owner.conn, title='Existing prerequisite', assignee='default', requires=[])
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: 'hub' if os.environ['HERMES_HOME'] == str(hub.home) else 'worker-1')
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    if not forwarded:
        monkeypatch.setattr(creation, 'plan', lambda *a, **kw: None)
    received = []
    def run(argv):
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2 canonical-home-v1', ''
        options = {k: json.loads(v) for k,v in (a.split('=',1) for i,a in enumerate(argv) if i and argv[i-1]=='--option')}
        incoming = {'title': 'Present human choice', 'body': 'Choose A or B now while the prerequisite proceeds.', 'assignee': 'default', **options}
        if diagnostic_restore:
            incoming['human_action_now'] = True
        intent = {'request_id': 'immediate-ask-contract', 'owner': {'gateway':'hub','profile':'default','board':'default'}, 'origin': {'gateway':'worker-1','profile':'default','session':'canonical-home-owner'}}
        hub.enter()
        try:
            received.append(json.loads(tools._handle_create(incoming, origin_intent=intent)))
        finally:
            worker.enter()
        return 3, 'queued', ''
    monkeypatch.setattr(placement, 'hub_runner', run)
    monkeypatch.setattr(placement, 'remote_state', lambda _: None)
    placement._help_cache.clear()
    out = json.loads(tools._handle_create({'title':'Present human choice','body':'Choose A or B now while the prerequisite proceeds.', 'assignee':'default','requires':[], 'parents':[parent], 'initial_status':'blocked','human_action_now':True,'human_request':REQUEST}))
    if forwarded:
        assert received and received[0].get('task_id'), (out, received)
    else:
        assert out.get('task_id'), out
