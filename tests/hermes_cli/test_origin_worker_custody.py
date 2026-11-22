"""Adopted worker custody intersects the real facade lease and native creation."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_native as native
from tests.tools import test_kanban_worker_created_chats as fixtures


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from agent.runtime_session_store import RuntimeSessionStore
    from gateway import session_kanban
    from hermes_cli import kanban_db_sync as sync, kanban_origin
    from hermes_state import SessionDB
    from hermes_state_runtime import (begin_runtime_epoch, register_worker_execution, adopt_worker_execution,
                                      mutate_worker_execution, worker_turn_lease)
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    from tests.hermes_cli.origin_custody_support import interactive_capture
    from tests.hermes_cli.kanban_sync_support import FakeLeaseClient
    monkeypatch.setattr(sync, '_LEASE_CLIENT', FakeLeaseClient())
    home = fixtures.make_home(tmp_path, monkeypatch)
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('here', []))
    monkeypatch.setattr(session_kanban, '_watch_submitter', lambda *a: None)
    monkeypatch.setattr(os, 'chdir', lambda *a: None)
    monkeypatch.setattr('agent.turn_liveness.resolve_turn_liveness_settings', lambda cfg: (None, 1.0))
    monkeypatch.setattr('agent.relay_cwd.resolve_relay_scope_cwds', lambda *a: ('', ''))
    with kbc.connect_closing() as conn:
        sync.set_mode(conn, 'primary')
        with interactive_capture(home, fixtures.CHAT) as captured:
            parent = kb.create_task(conn, title='root', assignee='default', native_origin=captured)
        assert native._receipt(conn, parent) is not None
        task = kb.claim_task(conn, parent, claimer='fixture-lock', ttl_seconds=3600)
        assert task is not None
        context = dict(task_id=parent, board='default', db=str(kb.kanban_db_path()), run_id=task.current_run_id,
                       claim_lock=task.claim_lock, workspace=str(home), workspaces_root=str(home), profile='default',
                       branch=None, tenant=None, goal_mode=False, max_runtime_seconds=3600)
    db = SessionDB(home / 'state.db')
    scope = dict(epoch=begin_runtime_epoch(db, instance_id='worker-fixture'), execution_id='native-worker',
                 session_id=fixtures.WORKER, generation=0)
    register_worker_execution(db, **scope, kind='kanban', adoption_secret='secret')
    adopted = adopt_worker_execution(db, **scope, adoption_secret='secret')
    def rpc(method, **params):
        if method == 'worker.turnLease':
            return worker_turn_lease(db, **params)
        return mutate_worker_execution(db, **params)
    store = RuntimeSessionStore(rpc, scope, home / 'outbox', adopted)
    frame = dict(scope=scope, home=str(home), policy=dict(source='kanban', kanban_json=json.dumps(context)))
    capability = session_kanban.bind_worker_context(frame)
    agent = _agent_with_db(store, session_id=fixtures.WORKER, platform='kanban')
    # Same explicit post-construction handoff as the private adopted worker.
    assert capability is not None
    capability.bind_agent(agent)
    agent._test_original_worker_custody = capability
    try:
        yield agent, store, db, context, frame
    finally:
        store.close()
        db.close()


def test_bound_capability_cannot_be_rebound_to_another_agent(worker):
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    agent, store, _, _, _ = worker
    other = _agent_with_db(store, session_id=agent.session_id, platform='kanban')
    with pytest.raises(ValueError, match='invalid_worker_origin_agent'):
        agent._test_original_worker_custody.bind_agent(other)


def test_bind_facade_and_creation_inherit_native_origin(worker, monkeypatch, capsys):
    from concurrent.futures import ThreadPoolExecutor
    from tools.thread_context import propagate_context_to_thread
    agent, store, _, context, _ = worker
    assert not hasattr(store, 'db_path')
    observed = []
    def loop(*args, **kwargs):
        capture = native.capture(context['task_id'])
        assert capture is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            child = pool.submit(propagate_context_to_thread(fixtures.create), 'tool', monkeypatch, capsys).result()
        with kbc.connect_closing() as conn:
            binding = native._receipt(conn, child)
            assert binding['origin']['session'] == fixtures.CHAT
            assert conn.execute('SELECT inherited_from FROM origin_native_receipts WHERE task_id=?', (child,)).fetchone()[0] == context['task_id']
        observed.append(capture)
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    assert agent.run_conversation('create')['final_response'] == 'ok'
    assert len(observed) == 1
    assert observed[0].validated() is None
    assert native.capture(context['task_id']) is None


@pytest.mark.parametrize('change', ['release', 'session', 'store'])
def test_release_during_final_owner_lease_read_refuses(worker, monkeypatch, change):
    from agent.origin_turn_custody import _current
    agent, store, _, context, _ = worker
    read = store.session_turn_lease_expires_at
    def loop(*args, **kwargs):
        lease = _current.get()[0]
        count = 0
        def releasing(*args):
            nonlocal count
            expiry = read(*args)
            count += 1
            if count == 2:
                if change == 'release':
                    lease.stop_refresher()
                elif change == 'session':
                    agent.session_id = 'different-session'
                else:
                    agent._session_db = object()
            return expiry
        monkeypatch.setattr(store, 'session_turn_lease_expires_at', releasing)
        assert native.capture(context['task_id']) is None
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('release during capture')


def test_adopted_execute_hands_capability_to_constructed_agent(tmp_path, monkeypatch):
    """Wiring proof alongside the real bind/facade/store/creation proof above."""
    from types import SimpleNamespace
    from gateway import session_kanban
    from tests.gateway.test_managed_worker_route_key import _execute
    from tests.gateway.test_managed_turn_contract import assignment
    frame, _ = assignment(tmp_path, monkeypatch)
    bound = []
    capability = SimpleNamespace(bind_agent=lambda agent: bound.append(agent))
    monkeypatch.setattr(session_kanban, 'bind_worker_context', lambda frame: capability)
    registered = []
    monkeypatch.setattr('agent.kanban_delegation_policy.register_root',
                        lambda agent, original: registered.append((agent, original)))
    def turns(agent, frame, history):
        assert bound == [agent]
        assert registered == [(agent, capability)]  # restriction lineage registered after the bind, before any turn
        return dict(final_response='ok', completed=True)
    monkeypatch.setattr(session_kanban, 'run_worker_turns', turns)
    _execute(frame, monkeypatch)
    assert len(bound) == 1
    assert len(registered) == 1
