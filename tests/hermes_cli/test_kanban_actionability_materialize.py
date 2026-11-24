"""Automatic intent delivery: bounded home custody, not execution authority. [daveey]"""
import copy
import json

import pytest

from hermes_cli import kanban_actionability_intents as intents
from hermes_cli import kanban_actionability_transport as transport
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_actionability_intents as journal
from tests.hermes_cli import test_kanban_actionability_recovery as recovery

live = journal.live


def setup(conn):
    event = dict(journal.new_park(conn))
    tid = event['task_id']
    hub = recovery.Hub(conn, tid)
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    task.update(uid=tid, deleted_at=None, current_run_uid=None, containment_episode=None)
    hub.data[tid]['task'] = task
    hub.data[tid]['events'] = [{**event, 'ord': 10, 'version': 1, 'deleted_at': None}]
    hub.now = event['created_at'] + 10
    return event, hub


def test_materialize_confirms_exact_source_deadline_without_lifecycle_write(live):
    event, hub = setup(live)
    before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (event['task_id'],)).fetchone())
    intents.materialize(live, hub, 'default')
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone())
    assert saved['state'] == 'confirmed'
    wire = hub.sent[0]
    assert wire['payload']['deadline_at'] == json.loads(event['payload'])['actionability_intent']['deadline_at']
    assert wire['payload']['facts']['source_uids'] == [{'uid': event['uid'], 'version': 1}]
    assert transport.command(live, saved['op_id'])['delivery_state'] == 'confirmed'
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (event['task_id'],)).fetchone()) == before
    intents.materialize(live, hub, 'default')
    assert len(hub.sent) == 1


@pytest.mark.parametrize('mutation', ['owner', 'payload', 'source', 'parents', 'run', 'stop'])
def test_changed_premise_stales_never_rebinds(live, mutation):
    event, hub = setup(live)
    data = hub.data[event['task_id']]
    mutations = {
        'owner': (data['task'], 'assignee', 'foreign'),
        'payload': (data['events'][0], 'payload', data['events'][0]['payload'] + ' '),
        'source': (data['events'][0], 'uid', 'successor'),
        'run': (data['task'], 'current_run_uid', 'new-run'),
        'stop': (data['task'], 'containment_episode', event['uid']),
    }
    if mutation == 'parents':
        hub.pages[0].update(rows=[{'uid': 'link', 'child_id': event['task_id'], 'parent_id': 'parent', 'deleted_at': None}], next='link')
        hub.data['parent'] = copy.deepcopy(data)
        hub.data['parent']['task'].update(id='parent', uid='parent')
        hub.data['parent']['task_id'] = 'parent'
        hub.data['parent']['events'][0]['task_id'] = 'parent'
    else:
        target, key, value = mutations[mutation]
        target[key] = value
    intents.materialize(live, hub, 'default')
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone())
    assert saved['state'] == 'stale'
    assert not hub.sent
    assert saved['op_id'] is None


def test_unavailable_inventory_transport_preserves_pending_custody(live):
    _, hub = setup(live)
    hub._call = None
    intents.materialize(live, hub, 'default')
    row = live.execute('SELECT state,reason FROM kanban_actionability_intents').fetchone()
    assert row['state'] == 'pending'
    assert row['reason'] == 'canonical execution inventory transport unavailable'
    assert not hub.sent


def test_local_replica_run_lag_cannot_stale_original_canonical_source(live):
    event, hub = setup(live)
    with kbc.write_txn(live):
        rid = live.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at) VALUES (?,'default','scheduled',1,2)",
                           (event['task_id'],)).lastrowid
        live.execute('UPDATE tasks SET current_run_id=? WHERE id=?', (rid, event['task_id']))
    intents.materialize(live, hub, 'default')
    assert live.execute('SELECT state FROM kanban_actionability_intents').fetchone()[0] == 'confirmed'
    assert len(hub.sent) == 1


def test_unknown_delivery_replays_original_bytes_after_restart_without_observing(live, monkeypatch):
    _, hub = setup(live)
    hub.answer = 'lost'
    intents.materialize(live, hub, 'default')
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone())
    assert saved['state'] == 'reserved'
    frozen = transport.command(live, saved['op_id'])['request_json']
    from hermes_cli import kanban_actionability_observation as observation
    monkeypatch.setattr(observation, 'observe', lambda *a, **kw: pytest.fail('rebound uncertain source'))
    hub.answer = 'applied'
    hub.now += 100
    intents.materialize(live, hub, 'default')
    assert hub.sent[0] == hub.sent[1]
    assert transport.command(live, saved['op_id'])['request_json'] == frozen
    assert live.execute('SELECT state FROM kanban_actionability_intents').fetchone()[0] == 'confirmed'


def test_unavailable_rotates_bounded_window_and_preserves_deadline(live, monkeypatch):
    event, hub = setup(live)
    with kbc.write_txn(live):
        for i in range(10):
            live.execute('INSERT INTO kanban_actionability_intents SELECT ?,task_id,source_json,source_kind,source_at,'
                         'deadline_at,state,reason,op_id,last_checked_at FROM kanban_actionability_intents WHERE event_uid=?',
                         (f'copy-{i}', event['uid']))
    from hermes_cli import kanban_actionability_observation as observation
    calls = []
    def unavailable(*a, **kw):
        assert not live.in_transaction
        calls.append(1)
        raise OSError('inventory unavailable')
    monkeypatch.setattr(observation, 'observe', unavailable)
    intents.materialize(live, hub, 'default')
    assert len(calls) == 8
    assert live.execute('SELECT COUNT(*) FROM kanban_actionability_intents WHERE last_checked_at>0').fetchone()[0] == 8
    intents.materialize(live, hub, 'default')
    assert live.execute('SELECT COUNT(*) FROM kanban_actionability_intents WHERE last_checked_at>0').fetchone()[0] == 11
    assert not hub.sent
    assert {r[0] for r in live.execute('SELECT deadline_at FROM kanban_actionability_intents')} == {
        json.loads(event['payload'])['actionability_intent']['deadline_at']}


@pytest.mark.parametrize('payload', [None, {}, []])
def test_missing_source_bytes_remain_pending_not_stale(live, payload):
    event, hub = setup(live)
    hub.data[event['task_id']]['events'][0]['payload'] = payload
    intents.materialize(live, hub, 'default')
    assert live.execute('SELECT state FROM kanban_actionability_intents').fetchone()[0] == 'pending'
    assert not hub.sent


def test_changed_journal_custody_is_not_rolled_back_by_error_handler(live, monkeypatch):
    event, hub = setup(live)
    from hermes_cli import kanban_actionability_observation as observation
    original = observation.observe
    def concurrent(*args, **kwargs):
        result = original(*args, **kwargs)
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_actionability_intents SET state='confirmed',op_id='other-writer' WHERE event_uid=?",
                         (event['uid'],))
        return result
    monkeypatch.setattr(observation, 'observe', concurrent)
    intents.materialize(live, hub, 'default')
    row = live.execute('SELECT state,op_id FROM kanban_actionability_intents').fetchone()
    assert tuple(row) == ('confirmed', 'other-writer')
    assert not hub.sent


def test_reservation_and_journal_state_roll_back_together(live, monkeypatch):
    _, hub = setup(live)
    original = transport.reserve
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('atomic reservation crash')
    monkeypatch.setattr(transport, 'reserve', crash)
    with pytest.raises(RuntimeError, match='atomic reservation crash'):
        intents.materialize(live, hub, 'default')
    assert live.execute('SELECT state FROM kanban_actionability_intents').fetchone()[0] == 'pending'
    assert not hub.sent
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_actionability_commands'").fetchone()


@pytest.mark.parametrize('canonical_status,expected', [('ready', 'confirmed'), ('done', 'stale')])
def test_real_materializer_checks_raw_parent_completion(live, canonical_status, expected):
    event, hub = setup(live)
    tid = event['task_id']
    data = json.loads(event['payload'])
    data['actionability_parent_ids'] = ['parent']
    data['actionability_parent_states'] = [{'id': 'parent', 'status': 'ready', 'containment_episode': None}]
    from hermes_cli.kanban_actionability_creation import intent_for
    data['actionability_intent'] = intent_for(hub.data[tid]['task'], event['kind'], data, now=event['created_at'])
    payload = json.dumps(data)
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_actionability_intents SET source_json=? WHERE event_uid=?', (payload, event['uid']))
    hub.data[tid]['events'][0]['payload'] = payload
    hub.pages[0].update(rows=[{'uid': 'link', 'child_id': tid, 'parent_id': 'parent', 'deleted_at': None}], next='link')
    parent = copy.deepcopy(hub.data[tid])
    parent.update(task_id='parent')
    parent['task'].update(id='parent', uid='parent', status=canonical_status)
    parent['events'][0]['task_id'] = 'parent'
    hub.data['parent'] = parent
    intents.materialize(live, hub, 'default')
    assert live.execute('SELECT state FROM kanban_actionability_intents').fetchone()[0] == expected
    assert len(hub.sent) == (1 if expected == 'confirmed' else 0)
