"""Definitive missing-episode rejection has a truthful verdict-free terminal path."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_retain as retain, kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_retain_receipt as receipts

live = receipts.live


def setup(live, monkeypatch, reason='missing_decision_episode', delivery='rejected'):
    owned, _, rows, hub = receipts.fixture(live, reason='decision_pending')
    actor = SimpleNamespace(task=owned['task_id'], run_uid=owned['run_uid'])
    monkeypatch.setattr(retain.actors, 'current_actor', lambda conn: actor)
    state = sync.sync_state(live)
    hub.ident.gateway = state['gateway_id']
    with kbc.write_txn(live):
        transport.record_result(live, {'op_id': owned['op_id']}, {'status': delivery, 'reason': reason})
    # Real push/translation; only the remote Board is simulated here.
    calls = []
    def ops(board, sent):
        if sent[0]['op'] == 'retain':
            return {'results': [{'client_op_id': owned['op_id'], 'status': delivery, 'reason': reason}]}
        calls.extend(sent)
        assert all(op['op'] == 'release' and op['outcome'] == 'released' for op in sent)
        assert all('unused_grant' not in op for op in sent)
        return {'results': [{'client_op_id': op['client_op_id'], 'status': 'applied'} for op in sent]}
    hub.ops = ops
    for (table, ident), value in rows.items():
        value.update(uid=ident, version=10, hlc='9999999999999-0000-server', origin_gateway=hub.ident.gateway)
    rows['task_runs', owned['run_uid']].update(outcome='released', metadata='{}')
    rows['tasks', owned['task_id']].update(status='ready', id=owned['task_id'], title='Fallback fixture',
                                         assignee='worker', lease_holder=None, lease_expires=None)
    return owned, actor, rows, hub, calls


def recover(live, hub, actor):
    data = {**receipts.support.PAYLOAD, 'reason': 'decision_pending'}
    return retain._primary(live, hub, 'default', actor, data)


def test_definitive_rejection_releases_without_a_fake_retain_or_crash(live, monkeypatch):
    owned, actor, _rows, hub, calls = setup(live, monkeypatch)
    result = recover(live, hub, actor)
    assert result['terminal_for_run'] is True and result['retained'] is False
    assert result['disposition'] == 'retain_rejected_run_released'
    assert result['dependency_satisfied'] is False
    after = transport.command(live, actor.run_uid)
    assert after['rejected_release_op_id'] and len(calls) == 1
    for key in ('request_json', 'request_sha256', 'op_id', 'event_uid', 'canonical_receipt_json'):
        assert after[key] == owned[key]
    assert after['delivery_state'] == 'rejected'
    assert live.execute('SELECT outcome FROM task_runs WHERE uid=?', (actor.run_uid,)).fetchone()[0] == 'released'
    assert live.execute('SELECT status,current_run_id FROM tasks WHERE id=?', (actor.task,)).fetchone()[:] == ('ready', None)
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('decision_retained','spend_budget_triaged')", (actor.task,)).fetchone()
    assert recover(live, hub, actor)['terminal_for_run'] is True
    assert len(calls) == 1  # ACK/readback recovery never creates another command.


@pytest.mark.parametrize('reason,delivery', [('episode_changed', 'rejected'), ('storage_error', 'rejected'),
    ('missing_decision_episode', 'deferred'), ('missing_decision_episode', 'applied')])
def test_unknown_transient_and_other_rejections_never_release(live, monkeypatch, reason, delivery):
    _, actor, _, hub, calls = setup(live, monkeypatch, reason, delivery)
    with pytest.raises(ValueError):
        recover(live, hub, actor)
    assert not calls
    assert transport.command(live, actor.run_uid)['rejected_release_op_id'] is None


@pytest.mark.parametrize('damage', ['live_run', 'wrong_task', 'unused', 'same_current', 'wrong_outcome', 'wrong_board'])
def test_ack_is_not_terminal_without_exact_independent_readback(live, monkeypatch, damage):
    _, actor, rows, hub, calls = setup(live, monkeypatch)
    run = rows['task_runs', actor.run_uid]
    changes = {
        'live_run': (run, 'ended_at', None),
        'wrong_task': (run, 'task_id', 't_other'),
        'unused': (run, 'metadata', json.dumps({'unused_grant': True})),
        'same_current': (rows['tasks', actor.task], 'current_run_uid', actor.run_uid),
        'wrong_outcome': (run, 'outcome', 'retained'),
    }
    if damage in changes:
        target, key, value = changes[damage]
        target[key] = value
    else:
        original = hub.row_evidence
        hub.row_evidence = lambda *a: {**original(*a), 'board': 'foreign'}
    with pytest.raises(ValueError):
        recover(live, hub, actor)
    assert len(calls) == 1
    assert transport.command(live, actor.run_uid)['delivery_state'] == 'rejected'


@pytest.mark.parametrize('damage', ['changed_payload', 'changed_digest', 'earlier_failure'])
def test_rejected_command_never_changes_intent_or_displaces_failure(live, monkeypatch, damage):
    from hermes_cli import kanban_failure_release as release
    owned, actor, _, hub, calls = setup(live, monkeypatch)
    data = {**receipts.support.PAYLOAD, 'reason': 'decision_pending'}
    if damage == 'changed_payload':
        data['summary'] = 'Different intent'
    elif damage == 'changed_digest':
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_failure_commands SET request_sha256='invalid'")
    else:
        with kbc.write_txn(live):
            rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (actor.run_uid,)).fetchone()[0]
            release.queue(live, actor.task, rid, outcome='crashed', error='real earlier failure')
    with pytest.raises(ValueError):
        retain._primary(live, hub, 'default', actor, data)
    assert not calls
    assert transport.command(live, actor.run_uid)['request_json'] == owned['request_json']


@pytest.mark.parametrize('successor', [False, True])
def test_exact_voluntary_live_claim_settlement_preserves_successor(live, monkeypatch, successor):
    import os
    from hermes_cli import kanban_db as kb
    _owned, actor, _rows, hub, _ = setup(live, monkeypatch)
    lock = kb._host_prefix() + ':1234'
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET claim_lock=?,worker_pid=? WHERE id=?', (lock, os.getpid(), actor.task))
        live.execute('UPDATE task_runs SET claim_lock=? WHERE uid=?', (lock, actor.run_uid))
    if successor:
        with kbc.write_txn(live):
            live.execute("UPDATE tasks SET claim_lock='successor' WHERE id=?", (actor.task,))
    assert recover(live, hub, actor)['terminal_for_run'] is True
    claim = live.execute('SELECT claim_lock FROM tasks WHERE id=?', (actor.task,)).fetchone()[0]
    assert claim == ('successor' if successor else None)


def test_lost_release_reply_recovers_one_command_via_independent_readback(live, monkeypatch):
    owned, actor, _, hub, calls = setup(live, monkeypatch)
    send = hub.ops
    def lost(board, sent):
        send(board, sent)
        raise OSError('reply lost after release')
    hub.ops = lost
    with pytest.raises(OSError, match='reply lost'):
        recover(live, hub, actor)
    first = transport.command(live, actor.run_uid)['rejected_release_op_id']
    hub.ops = send
    assert recover(live, hub, actor)['terminal_for_run'] is True
    assert transport.command(live, actor.run_uid)['rejected_release_op_id'] == first
    assert all(op['client_op_id'] == first for op in calls)
    assert transport.command(live, actor.run_uid)['request_json'] == owned['request_json']


def test_real_tool_adapter_reports_released_not_retained(live, monkeypatch):
    from contextlib import contextmanager
    from tools import kanban_retain_tools, kanban_tools
    from hermes_cli import kanban_sync_client as client
    _, actor, _, hub, _ = setup(live, monkeypatch)
    monkeypatch.setattr(retain.actors, 'bound_actor', lambda conn: actor)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'Hub', lambda ident: hub)
    hub.close = lambda: None
    @contextmanager
    def board(_):
        yield None, live
    monkeypatch.setattr(kanban_tools, '_board', board)
    args = {k: v for k, v in receipts.support.PAYLOAD.items() if k != 'version'}
    result = json.loads(kanban_retain_tools.handle({**args, 'reason': 'decision_pending'}))
    assert result['ok'] and result['terminal_for_run']
    assert result['retained'] is False and result['dependency_satisfied'] is False
    assert result['disposition'] == 'retain_rejected_run_released'


def test_stale_readback_cannot_overwrite_successor_lease_or_pending_writes(live, monkeypatch):
    from pathlib import Path
    _, actor, _, hub, _ = setup(live, monkeypatch)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    read = hub.row_evidence
    admitted = []
    def raced(board, table, uid):
        evidence = read(board, table, uid)
        if table == 'tasks' and not admitted:
            peer = kbc.connect(path)
            try:
                with kbc.write_txn(peer):
                    state = sync.sync_state(peer)
                    gate = sync.ClaimGate(9999999999, lease=sync.Lease(True, 'successor-run',
                        expires_ms=9999999999000, holder=state['gateway_id'], version=11),
                        ids=('default', state['gateway_id'], actor.task))
                    new_uid = sync.stage_claim(peer, actor.task, gate)
                    rid = peer.execute("INSERT INTO task_runs (uid,task_id,profile,status,started_at,claim_lock) "
                                       "VALUES (?,?,'worker','running',2,'successor')", (new_uid, actor.task)).lastrowid
                    peer.execute("UPDATE tasks SET status='running',current_run_id=?,claim_lock='successor' WHERE id=?", (rid, actor.task))
                    admitted.append(dict(peer.execute('SELECT * FROM tasks WHERE id=?', (actor.task,)).fetchone()))
            finally:
                peer.close()
        return evidence
    hub.row_evidence = raced
    assert recover(live, hub, actor)['terminal_for_run'] is True
    after = dict(live.execute('SELECT * FROM tasks WHERE id=?', (actor.task,)).fetchone())
    for key in ('status', 'current_run_id', 'claim_lock', 'lease_holder', 'lease_expires', 'lease_version'):
        assert after[key] == admitted[0][key], key
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid=? AND op='upsert'", (actor.task,)).fetchone()
    assert live.execute('SELECT outcome FROM task_runs WHERE uid=?', (actor.run_uid,)).fetchone()[0] == 'released'
