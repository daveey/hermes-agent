"""Runtime Stop -> managed settlement -> authoritative exit76, including a late child130."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway import session_managed_worker as managed
from gateway.run_runtime import stop_authority_turns
from gateway.session_kanban import worker_result
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_owner_recovery import managed_run_exit_code
from hermes_state_runtime import RuntimeStoreError


@pytest.mark.asyncio
@pytest.mark.parametrize('unacknowledged', [True, False])
async def test_runtime_stop_preserves_uncounted_exit_after_child_close(tmp_path, monkeypatch, unacknowledged):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='managed restart', assignee='default')
        kb.claim_task(conn, tid, claimer='host:1')
        task = kb.get_task(conn, tid)
        assert task is not None
        params = {'task_id': tid, 'run_id': task.current_run_id, 'claim_lock': 'host:1'}
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_pid=79101 WHERE id=?', (tid,))
    context = dict(params, db=str(tmp_path / 'kanban.db'))
    policy = SimpleNamespace(source='kanban', kanban_json=json.dumps(context))
    row = {'admission_id': 'a', 'generation': 1, 'principal_id': 'p'}
    ref = SimpleNamespace(session_id='s')
    trace = []
    stream = SimpleNamespace(lock=threading.RLock(), publish=lambda *a: None)
    authority = SimpleNamespace(pending_results={}, _managed_workers={}, _mutation_tasks=set(),
        pending_stops={}, executing={'s': 1}, waiters={}, adopt_agent=lambda *a: None,
        _publish_pending=lambda *a: None,
        sessions={'s': SimpleNamespace(event_stream=stream, controls=SimpleNamespace(snapshot=lambda *a: None))})
    class Worker(managed.ManagedWorker):
        def __init__(self):
            super().__init__(SimpleNamespace(wait=lambda *a: 0))
            self.frames = 0
            self.writer = SimpleNamespace(start=lambda: None)
        async def next_frame(self, *args, **kwargs):
            self.frames += 1
            if self.frames == 1:
                return {}
            if self.frames == 2:
                stop_authority_turns(authority, reason='Gateway restarting')
                assert self.stop_cause == 'Gateway restarting' and self.stop.is_set()
                if unacknowledged:
                    raise RuntimeStoreError('managed_worker_stopped')
                return {'type': 'result', 'result': {'interrupted': True, 'final_response': ''}}
            return {'type': 'finished'}
        def send(self, frame):
            pass
        def close(self):
            # A child can book130 even after receiving Stop but before its pipe closes.
            with kbc.connect() as conn, kb.write_txn(conn):
                kb._append_event(conn, tid, 'worker_result',
                    {'pid': 79102, 'claim_lock': 'host:1', 'exit_code': 130}, run_id=params['run_id'])
                kb._append_event(conn, tid, 'worker_bound',
                    {'pid': 79102, 'claim_lock': 'host:1'}, run_id=params['run_id'])
            trace.append('closed')
            self.closed.set()
    worker = Worker()
    async def spawn(*args):
        authority._managed_workers['s'] = worker
        return worker, worker.process
    async def text(*args):
        return 'prompt', None
    monkeypatch.setattr('gateway.session_context_refs.managed_turn_text', text)
    monkeypatch.setattr('gateway.session_policy.workspace_ready', lambda _: True)
    monkeypatch.setattr(managed, '_spawn_registered_worker', spawn)
    monkeypatch.setattr(managed, 'reserve_admission_worker', lambda *a, **k:
        {'pid': 79102, 'birth': 1, 'session_id': 's', 'execution_id': 'e', 'secret': ''})
    monkeypatch.setattr(managed, '_bootstrap', lambda *a: {'api_key': ''})
    monkeypatch.setattr(managed, 'accept_result', lambda result: (result, {}))
    monkeypatch.setattr(managed, '_settled_session_usage', lambda *a: {})
    monkeypatch.setattr(managed, '_record_lost_attempt', lambda *a: None)
    async def lose(*args):
        assert trace == ['closed']
        assert worker_result(context['db'], params)['exit_code'] == 76
        trace.append('unknown published')
    monkeypatch.setattr(managed, '_settle_lost', lose)
    if unacknowledged:
        with pytest.raises(managed.ManagedExecutionUnknown):
            await managed.admit_managed(authority, ref, row, policy)
    else:
        assert await managed.admit_managed(authority, ref, row, policy) == ''
        assert authority.pending_results['a']['runtime_stop_cause'] == 'Gateway restarting'
    with kbc.connect() as conn:
        # Even a client that missed the drain and exits1 cannot override the owner's76.
        assert managed_run_exit_code(conn, tid, 79101, 'host:1') == 76
    assert trace.count('closed') == 1
