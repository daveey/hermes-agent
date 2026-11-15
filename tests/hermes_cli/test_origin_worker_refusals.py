"""Refusal and transaction fences for adopted native worker inheritance."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_native as native
from tests.hermes_cli import test_origin_worker_custody as helpers

worker = helpers.worker


@pytest.mark.parametrize('change', ['task_expired', 'run_expired', 'task_status', 'run_status', 'task_run',
                                  'task_lock', 'run_lock', 'run_task', 'run_ended', 'bound_pid', 'bound_lock'])
@pytest.mark.parametrize('phase', ['capture', 'transaction'])
def test_claim_changes_revoke_capture_and_creation(worker, monkeypatch, change, phase):
    agent, _, _, context, _ = worker
    def change_claim(conn):
        statements = {
            'task_expired': ('UPDATE tasks SET claim_expires=0 WHERE id=?', context['task_id']),
            'run_expired': ('UPDATE task_runs SET claim_expires=0 WHERE id=?', context['run_id']),
            'task_status': ("UPDATE tasks SET status='done' WHERE id=?", context['task_id']),
            'run_status': ("UPDATE task_runs SET status='done' WHERE id=?", context['run_id']),
            'task_run': ('UPDATE tasks SET current_run_id=NULL WHERE id=?', context['task_id']),
            'task_lock': ("UPDATE tasks SET claim_lock='other' WHERE id=?", context['task_id']),
            'run_lock': ("UPDATE task_runs SET claim_lock='other' WHERE id=?", context['run_id']),
            'run_task': ("UPDATE task_runs SET task_id='wrong-task' WHERE id=?", context['run_id']),
            'run_ended': ('UPDATE task_runs SET ended_at=1 WHERE id=?', context['run_id']),
        }
        if change.startswith('bound_'):
            from gateway import session_kanban
            payload = dict(pid=-1 if change == 'bound_pid' else os.getpid(),
                           claim_lock='other' if change == 'bound_lock' else context['claim_lock'])
            assert session_kanban._bound_worker_matches(conn, context)
            kb._append_event(conn, context['task_id'], 'worker_bound', payload, run_id=context['run_id'])
        else:
            sql, value = statements[change]
            # The other task exists so foreign-key enforcement remains enabled.
            if change == 'run_task':
                other = kb.create_task(conn, title='different creator')
                conn.execute('UPDATE task_runs SET task_id=? WHERE id=?', (other, value))
            else:
                conn.execute(sql, (value,))
    def loop(*args, **kwargs):
        captured = native.capture(context['task_id'])
        assert captured is not None
        with kbc.connect_closing() as conn:
            if phase == 'capture':
                with kb.write_txn(conn):
                    change_claim(conn)
                assert native.capture(context['task_id']) is None
            else:
                # The mutation is deliberately uncommitted: a separate reader
                # sees the OLD valid claim, whereas the creation connection must
                # reject it. This also exercises nested create_task transactions.
                with kb.write_txn(conn):
                    change_claim(conn)
                    child = kb.create_task(conn, title='no authority', native_origin=captured)
                    assert native._receipt(conn, child) is None
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('test claim fence')


@pytest.mark.parametrize('change', ['wrong_creator', 'wrong_agent', 'store', 'session', 'scope', 'lease_expired',
                                  'lease_revoked', 'delegated', 'pid', 'home', 'profile', 'gateway', 'nested',
                                  'owner_unavailable', 'epoch'])
def test_worker_custody_intersection(worker, monkeypatch, change):
    from agent import origin_turn_custody
    from agent.delegation_context import delegated_child_context
    from types import SimpleNamespace
    agent, store, db, context, _ = worker
    def loop(*args, **kwargs):
        captured = native.capture(context['task_id'])
        assert captured is not None
        if change == 'wrong_creator':
            assert native.capture('other-task') is None
            return dict(final_response='ok', messages=[], failed=False)
        if change in ('delegated', 'nested'):
            manager = delegated_child_context() if change == 'delegated' else origin_turn_custody.scope(None)
            with manager:
                assert native.capture(context['task_id']) is None
                assert captured.validated() is None
            assert captured.validated() is not None
            return dict(final_response='ok', messages=[], failed=False)
        from agent.runtime_session_store import WorkerPersistenceError
        lease = origin_turn_custody._current.get()[0]
        def unavailable(*args):
            raise WorkerPersistenceError('owner_unavailable')
        def replace_epoch():
            from hermes_state_runtime import begin_runtime_epoch
            begin_runtime_epoch(db, instance_id='replacement-owner')
            # The replaced owner cannot accept old worker writes. Cleanup belongs
            # to the fixture's new owner, not the stale worker's write journal.
            monkeypatch.setattr(store, 'release_session_turn_lease',
                lambda sid, holder, **kw: db.release_session_turn_lease(sid, holder))
        with monkeypatch.context() as patch:
            mutations = {
                'wrong_agent': lambda: patch.setattr(lease, 'agent', SimpleNamespace(**vars(agent))),
                'store': lambda: patch.setattr(agent, '_session_db', object()),
                'session': lambda: patch.setattr(agent, 'session_id', 'different-session'),
                'scope': lambda: patch.setitem(store.scope, 'generation', 100),
                'lease_expired': lambda: patch.setattr(store, 'session_turn_lease_expires_at', lambda *a: 0),
                'lease_revoked': lambda: db.release_session_turn_lease(agent.session_id, agent._active_session_turn_lease_holder),
                'owner_unavailable': lambda: patch.setattr(store, 'session_turn_lease_expires_at', unavailable),
                'epoch': replace_epoch,
                'pid': lambda: patch.setattr(os, 'getpid', lambda: -1),
                'home': lambda: patch.setenv('HERMES_HOME', str(db.db_path.parent / 'other')),
                'profile': lambda: patch.setattr('hermes_cli.profiles.get_active_profile_name', lambda: 'other'),
                'gateway': lambda: patch.setattr('hermes_cli.kanban_origin.gateway_names', lambda: ('other', [])),
            }
            mutations[change]()
            assert native.capture(context['task_id']) is None
            assert captured.validated() is None
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('test intersection')


@pytest.mark.parametrize('field', ['task_id', 'run_id', 'claim_lock'])
def test_bind_rejects_changed_adopted_claim(worker, field):
    from gateway.session_kanban import bind_worker_context
    from hermes_state_runtime import RuntimeStoreError
    _, _, _, context, frame = worker
    altered = dict(context)
    altered[field] = 999999 if field == 'run_id' else 'wrong'
    frame = dict(frame, policy=dict(frame['policy'], kanban_json=json.dumps(altered)))
    with pytest.raises(RuntimeStoreError):
        bind_worker_context(frame)


@pytest.mark.parametrize('change', ['missing', 'namespace', 'foreign_origin'])
def test_parent_requires_exact_local_native_receipt(worker, monkeypatch, change):
    agent, _, _, context, _ = worker
    def loop(*args, **kwargs):
        captured = native.capture(context['task_id'])
        assert captured is not None
        with kbc.connect_closing() as conn:
            binding = native._receipt(conn, context['task_id'])
            if change == 'missing':
                conn.execute('DELETE FROM origin_native_receipts WHERE task_id=?', (context['task_id'],))
                # Real synced discovery rows remain; they must not restore authority.
                from hermes_cli import kanban_chat_links
                kanban_chat_links.reconcile(conn, 'default', 'here', ledger=False)
            else:
                if change == 'namespace':
                    binding['owner']['profile'] = 'other'
                    binding['task_record_key'] = 'other/default/' + context['task_id']
                else:
                    binding['origin']['gateway'] = 'foreign'
                conn.execute('UPDATE origin_native_receipts SET binding_json=? WHERE task_id=?',
                             (json.dumps(binding), context['task_id']))
            conn.commit()
            assert captured.validated() is not None  # Claim is still live.
            child = kb.create_task(conn, title='no parent authority', native_origin=captured)
            assert native._receipt(conn, child) is None
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('test parent receipt')


@pytest.mark.parametrize('change', ['claim', 'turn'])
def test_revoke_during_parent_receipt_read_refuses(worker, monkeypatch, change):
    agent, _, db, context, _ = worker
    read = native._receipt
    def loop(*args, **kwargs):
        captured = native.capture(context['task_id'])
        assert captured is not None
        def revoke(conn, task_id):
            binding = read(conn, task_id)
            if task_id == context['task_id']:
                if change == 'claim':
                    conn.execute('UPDATE tasks SET claim_expires=0 WHERE id=?', (task_id,))
                else:
                    db.release_session_turn_lease(agent.session_id, agent._active_session_turn_lease_holder)
            return binding
        monkeypatch.setattr(native, '_receipt', revoke)
        with kbc.connect_closing() as conn:
            child = kb.create_task(conn, title='revoked at receipt read', native_origin=captured)
            assert read(conn, child) is None
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('revoke during persistence')
