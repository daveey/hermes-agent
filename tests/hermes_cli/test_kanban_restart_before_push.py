"""A fresh producer exits before push; its durable card/edge reach Board on restart.

The transport is FakeService, not production Board. This is preventative coverage,
not evidence the historical incident lost an outbox operation.
"""
import json
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws


def test_worker_created_hub_parent_survives_process_exit_before_push(gws, monkeypatch):
    hub, worker = gws
    worker.enter()
    worker.conn.close()
    code = '''
import json
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
conn = kbc.connect()
parent = kb.create_task(conn, title="hub helper", assignee="default", runs_on=["machine:hub"])
child = kb.create_task(conn, title="dependent worker", assignee="default", parents=[parent])
print(json.dumps([parent, child]))
conn.close()
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, encoding='utf-8', timeout=30, check=True)
    parent, child = json.loads(result.stdout.strip().splitlines()[-1])
    worker.conn = kbc.connect(worker.home / 'kanban.db')
    row = worker.conn.execute('SELECT runs_on FROM tasks WHERE id = ?', (parent,)).fetchone()
    assert json.loads(row[0]) == ['machine:hub']
    worker.round()
    hub.round()
    hub.enter()
    monkeypatch.setattr(wn, 'pickup_mode', lambda: 'explicit')
    assert wn.pickup(hub.conn, budget=1) == [parent]
    assert kb.get_task(hub.conn, parent).title == 'hub helper'
    worker.enter()
    assert kb.parent_states(worker.conn, child) == [(parent, 'ready')]
    assert kb.get_task(worker.conn, child).status == 'todo'
