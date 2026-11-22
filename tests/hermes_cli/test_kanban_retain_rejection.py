"""Definitive retain rejection keeps custody, not a permanent observer exemption."""
import json
import signal
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync, kanban_failure_release as release
from hermes_cli import kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_failure_transport as support
from tests.hermes_cli import test_kanban_retain_receipt as receipts
from tests.hermes_cli.test_kanban_retain_transport import reserve

live = support.live


def reject(conn, owned):
    with kbc.write_txn(conn):
        transport.record_result(conn, {'op_id': owned['op_id']}, {'status': 'rejected', 'reason': 'episode_changed'})


def running(conn, monkeypatch, state):
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = support._running(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=987654,claim_lock=?,max_runtime_seconds=1,started_at=1 WHERE id=?",
                     (kb._host_prefix() + 'fixture', tid))
        conn.execute('DELETE FROM kanban_sync_outbox')
    owned = reserve(conn, tid, rid)
    if state == 'rejected':
        reject(conn, owned)
    else:
        with kbc.write_txn(conn):
            conn.execute('UPDATE kanban_failure_commands SET delivery_state=?', (state,))
    monkeypatch.setattr(kb, '_pid_alive', lambda *a: False)
    monkeypatch.setattr(kb, '_worker_alive', lambda *a: False)
    monkeypatch.setattr(dispatch, '_worker_alive', lambda *a: False)
    monkeypatch.setattr(dispatch, '_poll_worker_exit', lambda *a: None)
    monkeypatch.setattr(recovery, 'owner_reclaim_blocked', lambda *a, **kw: False)
    monkeypatch.setattr(recovery, 'owner_reclaim_blocked_after_kill', lambda *a, **kw: False)
    monkeypatch.setattr(recovery, 'managed_run_exit_code', lambda *a: 1)
    monkeypatch.setattr(recovery, 'apply_attempt_note', lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, 'victims', lambda *a: (None, []))
    monkeypatch.setattr(dispatch._host_reboot, 'note', lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, 'comment_culprit', lambda *a: None)
    monkeypatch.setattr(dispatch._owner_death, 'note', lambda *a: None)
    monkeypatch.setattr(dispatch, '_classify_dead_worker', lambda *a, **kw:
                        dispatch._DeadWorker('crashed', 1, 'fixture exit', 'crashed', {'exit_code': 1}))
    return tid, rid, transport.command(conn, owned['run_uid'])


@pytest.mark.parametrize('path', ['timeout', 'crash', 'failed_result'])
@pytest.mark.parametrize('state', ['rejected', 'pending', 'unknown', 'acknowledged', 'confirmed'])
def test_actual_observers_recover_rejection_once_without_replacing_retain(live, monkeypatch, path, state):
    tid, rid, before = running(live, monkeypatch, state)
    signals = []
    if path == 'timeout':
        invoke = lambda: dispatch.enforce_max_runtime(live, signal_fn=lambda *a: signals.append(a))
    elif path == 'crash':
        invoke = lambda: dispatch.detect_crashed_workers(live)
    else:
        from hermes_cli import kanban_failed_result_observer as observer
        with kbc.write_txn(live):
            kb._append_event(live, tid, 'worker_result', {'exit_code': 1}, run_id=rid)
        context = {'db': str(live.execute('PRAGMA database_list').fetchone()[2]), 'task_id': tid,
                   'run_id': rid, 'claim_lock': kb._host_prefix() + 'fixture', 'writer_pid': None,
                   'writer_birth': None, 'board': 'default'}
        monkeypatch.setattr(observer, '_generation', lambda conn: context['db'])
        monkeypatch.setattr(observer, '_finalize', lambda conn, ctx, path:
                            bool(dispatch.detect_crashed_workers(conn)))
        invoke = lambda: observer.observe(context)
    invoke()
    after = transport.command(live, before['run_uid'])
    for key in ('op_id', 'request_uid', 'event_uid', 'request_json', 'request_sha256', 'delivery_state'):
        assert after[key] == before[key]
    rows = list(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'"))
    assert len(rows) == (1 if state == 'rejected' else 0)
    assert signals == ([(987654, signal.SIGTERM)] if path == 'timeout' and state == 'rejected' else [])
    if state == 'rejected':
        assert json.loads(rows[0]['fields'])['outcome'] == ('timed_out' if path == 'timeout' else 'crashed')
        assert after['rejected_release_op_id'] == rows[0]['op_id']
        assert release.queued(live, rid)
        invoke()
        assert live.execute("SELECT count(*) FROM kanban_sync_outbox WHERE op='release'").fetchone()[0] == 1
    assert live.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'running'


def test_fallback_atomic_dedup_survives_ack_and_transport_identity_remap(live):
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    reject(live, owned)
    with pytest.raises(RuntimeError), kbc.write_txn(live):
        release.queue(live, tid, rid, outcome='crashed', error='first')
        raise RuntimeError('rollback')
    assert not release.queued(live, rid)
    with kbc.write_txn(live):
        release.queue(live, tid, rid, outcome='crashed', error='first')
        release.queue(live, tid, rid, outcome='timed_out', error='second')
    row = live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'").fetchone()
    assert row is not None and json.loads(row['fields'])['outcome'] == 'crashed'
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET op_id='remapped' WHERE op='release'")
        release.queue(live, tid, rid, outcome='crashed', error='third')
        live.execute("DELETE FROM kanban_sync_outbox WHERE op='release'")
        release.queue(live, tid, rid, outcome='crashed', error='after ACK')
    assert release.queued(live, rid) and release.command_owned(live, rid)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='release'").fetchone()
    assert transport.command(live, owned['run_uid'])['request_json'] == owned['request_json']


@pytest.mark.parametrize('late', ['applied', 'deferred', 'rejected'])
@pytest.mark.parametrize('first', ['rejected', 'confirmed'])
def test_delayed_result_cannot_reverse_rejection_or_confirmation(live, late, first):
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_failure_commands SET delivery_state=?', (first,))
        transport.record_result(live, {'op_id': owned['op_id']}, {'status': late})
    assert transport.command(live, owned['run_uid'])['delivery_state'] == first


@pytest.mark.parametrize('boundary', ['reserve', 'outbound', 'confirm'])
def test_rejected_retain_is_sticky_at_every_send_boundary(live, boundary):
    owned, _receipt, _rows, hub = receipts.fixture(live)
    reject(live, owned)
    with pytest.raises(ValueError, match='reject'):
        if boundary == 'reserve':
            reserve(live, owned['task_id'], live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0])
        elif boundary == 'outbound':
            transport.outbound(live, dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='retain'").fetchone()))
        else:
            transport.confirm(live, hub, 'default', owned['run_uid'])
    assert not hub.calls


@pytest.mark.parametrize('matching', [True, False])
def test_confirm_records_only_matching_definitive_rejection(live, matching):
    owned, _receipt, _rows, hub = receipts.fixture(live)
    hub.ops = lambda *a: {'results': [{'client_op_id': owned['op_id'] if matching else 'foreign',
                                     'status': 'rejected', 'reason': 'episode_changed'}]}
    with pytest.raises(ValueError):
        transport.confirm(live, hub, 'default', owned['run_uid'])
    assert transport.command(live, owned['run_uid'])['delivery_state'] == ('rejected' if matching else 'pending')


@pytest.mark.parametrize('state', ['rejected', 'pending', 'unknown', 'acknowledged', 'confirmed'])
def test_real_child_result_writer_and_unmocked_observer_recover_rejection(live, monkeypatch, state):
    from hermes_cli import kanban_failed_result_observer as observer
    from tests.hermes_cli.test_kanban_failed_result_reconcile import _failed_child
    tid, rid = support._running(live)
    claim = kb._host_prefix() + 'fixture'
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET claim_lock=? WHERE id=?', (claim, tid))
    owned = reserve(live, tid, rid)
    if state == 'rejected':
        reject(live, owned)
    else:
        with kbc.write_txn(live):
            live.execute('UPDATE kanban_failure_commands SET delivery_state=?', (state,))
    context = {'db': str(live.execute('PRAGMA database_list').fetchone()[2]),
               'task_id': tid, 'run_id': rid, 'claim_lock': claim, 'board': 'default'}
    _failed_child(live, context)
    sleeps = []
    def bounded_sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= 2, 'observer failed to settle command custody'
    monkeypatch.setattr(observer, 'time', SimpleNamespace(sleep=bounded_sleep, monotonic=time.monotonic))
    observer.observe(context)
    rows = list(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'"))
    assert len(rows) == (1 if state == 'rejected' else 0)
    if rows:
        assert json.loads(rows[0]['fields'])['outcome'] == 'crashed'
    assert live.execute('SELECT outcome FROM task_runs WHERE id=?', (rid,)).fetchone()[0] is None
    assert transport.command(live, owned['run_uid'])['request_json'] == owned['request_json']


def test_rejected_fallback_neutral_effects_wait_for_feed_and_drain_once(live):
    from hermes_cli import kanban_neutral_release as neutral
    from tests.hermes_cli.test_kanban_retain_readers import dead_run
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    reject(live, owned)
    assert not neutral.pending(live) and not release.queued(live, rid)
    dead = dead_run(rid)
    dead.rate_limited, dead.owner_restart = False, True
    dead.event_kind = 'owner_restart'
    row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    with kbc.write_txn(live):
        assert neutral.queue(live, row, dead)
    assert neutral.pending(live) and neutral.drain(live) == []
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox')  # ACK alone is not feed proof.
    assert neutral.drain(live) == []
    with kbc.write_txn(live):
        live.execute("UPDATE task_runs SET ended_at=2,outcome='reclaimed' WHERE id=?", (rid,))
        kb._append_event(live, tid, 'owner_restart', {}, run_id=rid)
    assert len(neutral.drain(live)) == 1
    assert neutral.drain(live) == []


@pytest.mark.parametrize('failure', ['capability', 'actor', 'reserve', 'push', 'confirm'])
def test_primary_hub_closes_on_every_preterminal_error(live, monkeypatch, failure):
    from hermes_cli import kanban_retain as retain, kanban_sync_client as client
    tid, rid = support._running(live)
    actor = SimpleNamespace(task=tid, run_uid=sync.run_uid(live, rid))
    monkeypatch.setattr(retain.actors, 'bound_actor', lambda conn: actor)
    monkeypatch.setattr(retain.actors, 'current_actor', lambda conn: actor)
    state = sync.sync_state(live)
    closed = []
    hub = SimpleNamespace(ident=SimpleNamespace(gateway=state['gateway_id']), close=lambda: closed.append(True))
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'Hub', lambda ident: hub)
    monkeypatch.setattr(transport, 'capability', lambda *a: {})
    def fail(*a, **kw):
        raise ValueError('isolated ' + failure)
    targets = {'capability': (transport, 'capability'), 'actor': (retain.actors, 'current_actor'),
               'reserve': (transport, 'reserve'), 'push': (client, 'push'), 'confirm': (transport, 'confirm')}
    if failure == 'confirm':
        monkeypatch.setattr(client, 'push', lambda *a, **kw: None)
    monkeypatch.setattr(*targets[failure], fail)
    with pytest.raises(ValueError, match='isolated ' + failure):
        retain.retain(live, board=state['board_id'], **{k: v for k, v in receipts.support.PAYLOAD.items() if k != 'version'})
    assert closed == [True]
