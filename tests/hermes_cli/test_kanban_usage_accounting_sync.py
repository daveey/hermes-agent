"""Actual source outbox/feed remaps usage by UID after Board-primary release. [daveey]"""
import os
from types import SimpleNamespace

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_budget as spending
from tests.hermes_cli import test_kanban_worker_node as helpers

kanban_home = spending.kanban_home
svc = helpers.svc
gws = helpers.gws


@pytest.mark.parametrize('reverse', [False, True])
def test_release_then_late_duplicate_receipt_through_real_sync(gws, monkeypatch, reverse):
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title='isolated accounting transport', assignee='default', runs_on=['linux'])
    hub.round()
    worker.round()
    worker.enter()
    unrelated = kb.create_task(worker.conn, title='unrelated local fixture history', assignee='default')
    spending._run(worker.conn, unrelated, usage={'cache_read_tokens': 0})
    from hermes_cli import kanban_worker_node as wn
    assert wn.pickup(worker.conn, budget=1) == [tid]
    assert kb.claim_task(worker.conn, tid)
    task = kb.get_task(worker.conn, tid)
    rid = task.current_run_id
    uid = worker.conn.execute('SELECT uid FROM task_runs WHERE id=?', (rid,)).fetchone()[0]
    context = {'db': str(worker.home/'kanban.db'), 'task_id': tid, 'run_id': rid, 'claim_lock': task.claim_lock}
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, 'owner_admitted', {'session_id': 'isolated-source'}, run_id=rid)
        kb._append_event(worker.conn, tid, 'worker_bound', {'pid': os.getpid(), 'claim_lock': task.claim_lock}, run_id=rid)
    worker.round()
    dispatch._record_task_failure(worker.conn, tid, '', outcome='released', release_claim=True, end_run=True,
                                  infrastructure=True)
    worker.round()
    hub.round()
    hub.enter()
    assert kb.get_task(hub.conn, tid).status == 'ready'
    assert kb.claim_task(hub.conn, tid) is None
    assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
    home_rid = hub.conn.execute('SELECT id FROM task_runs WHERE uid=?', (uid,)).fetchone()[0]
    # IDs are gateway-local; equality would not prove the FK mapping boundary.
    assert home_rid != rid
    worker.enter()
    agent = SimpleNamespace(session_input_tokens=10, session_output_tokens=2,
                            session_cache_read_tokens=100, session_cache_write_tokens=0)
    session_kanban._record_worker_result(context, 0, '', agent=agent, extra={'continuable': True})
    agent.session_cache_read_tokens = 11034880
    session_kanban._record_worker_result(context, 0, '', agent=agent)
    session_kanban._record_worker_result(context, 0, '', agent=agent)
    from tests.hermes_cli.kanban_sync_fake_board import FakeHub
    original_ops = FakeHub.ops
    deliveries = []

    def wire(client, board, ops):
        if client.gateway == worker.name:
            assert len(ops) == 3 and all(op['table'] == 'task_events' for op in ops)
            # Reorder real serialized source operations at the transport, not
            # injected canonical totals. Retry those exact immutable op IDs too.
            batch = list(reversed(ops)) if reverse else ops
            result = original_ops(client, board, batch)
            retry = original_ops(client, board, batch)
            deliveries.append(retry)
            return result
        return original_ops(client, board, ops)

    with monkeypatch.context() as patch:
        patch.setattr(FakeHub, 'ops', wire)
        worker.round()
    assert len(deliveries) == 1
    assert all(item['dup'] for item in deliveries[0]['results'])
    hub.round()
    hub.enter()
    assert budget.spend(hub.conn, tid).tokens['cache_read_tokens'] == 11034880
    assert kb.claim_task(hub.conn, tid) is not None
