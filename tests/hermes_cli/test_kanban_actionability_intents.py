"""Source-event custody is atomic, immutable, local and home-only. [daveey]"""
import json
import socket

from hermes_cli import kanban_db_sync as sync

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


def new_park(conn):
    tid = kb.create_task(conn, title='creation intent journal', assignee='default')
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on='fixture' WHERE id=?", (tid,))
    assert kb.schedule_task(conn, tid, reason='finite window', clock_only=True,
                            awaiting={'kind': 'clock', 'why': 'bounded journal proof'})
    return conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()


def test_new_local_source_before_first_board_ack_is_journaled(live):
    tid = kb.create_task(live, title='pre-ack source custody', assignee='default')
    assert live.execute('SELECT created_on FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
    assert kb.schedule_task(live, tid, reason='finite window', clock_only=True,
                            awaiting={'kind': 'clock', 'why': 'pre-ack creation proof'})
    source = live.execute("SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents WHERE event_uid=?', (source['uid'],)).fetchone())
    assert saved['source_json'] == source['payload']
    assert saved['state'] == 'pending'
    assert live.execute('SELECT created_on FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None


@pytest.mark.parametrize('mutation', ['applying', 'gateway', 'empty_home', 'version', 'outbox_missing', 'outbox_delete', 'outbox_state'])
def test_unconfirmed_local_home_requires_all_original_insert_pins(live, mutation):
    from hermes_cli import kanban_actionability_intents as intents
    source = new_park(live)
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_actionability_intents')
        live.execute('UPDATE tasks SET created_on=NULL WHERE id=?', (source['task_id'],))
        mutations = {
            'applying': ('UPDATE kanban_sync_state SET applying=1', ()),
            'gateway': ("UPDATE kanban_sync_state SET gateway_id=' '", ()),
            'empty_home': ("UPDATE tasks SET created_on='' WHERE id=?", (source['task_id'],)),
            'version': ('UPDATE tasks SET sync_version=1 WHERE id=?', (source['task_id'],)),
            'outbox_missing': ('DELETE FROM kanban_sync_outbox WHERE row_uid=?', (source['uid'],)),
            'outbox_delete': ("UPDATE kanban_sync_outbox SET op='delete' WHERE row_uid=?", (source['uid'],)),
            'outbox_state': ("UPDATE kanban_sync_outbox SET state='rejected' WHERE row_uid=?", (source['uid'],)),
        }
        live.execute(*mutations[mutation])
        intents.capture(live, source['uid'])
    assert not live.execute('SELECT 1 FROM kanban_actionability_intents').fetchone()


def test_append_captures_exact_uid_and_bytes_and_replay_does_not_renew(live):
    from hermes_cli import kanban_actionability_intents as intents
    source = new_park(live)
    assert source['uid']
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents WHERE event_uid=?', (source['uid'],)).fetchone())
    assert saved['source_json'] == source['payload']
    assert saved['deadline_at'] == json.loads(source['payload'])['actionability_intent']['deadline_at']
    assert saved['state'] == 'pending'
    with kbc.write_txn(live):
        intents.capture(live, source['uid'])
    assert dict(live.execute('SELECT * FROM kanban_actionability_intents WHERE event_uid=?', (source['uid'],)).fetchone()) == saved


def test_journal_failure_rolls_back_lifecycle_and_source(live, monkeypatch):
    from hermes_cli import kanban_actionability_intents as intents
    tid = kb.create_task(live, title='rollback park', assignee='default')
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET created_on='fixture' WHERE id=?", (tid,))
    def fail(*args):
        raise RuntimeError('journal insert refused')
    monkeypatch.setattr(intents, 'capture', fail)
    with pytest.raises(RuntimeError, match='journal insert refused'):
        kb.schedule_task(live, tid, reason='finite window', clock_only=True,
                         awaiting={'kind': 'clock', 'why': 'atomicity proof'})
    assert live.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'ready'
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()


def imported_source(conn, *, uid='imported-intent', mutate=None):
    source = dict(new_park(conn))
    with kbc.write_txn(conn):
        conn.execute('DELETE FROM kanban_actionability_intents')
    payload = json.loads(source['payload'])
    if mutate:
        mutate(payload)
    raw = json.dumps(payload)
    change = sync.RemoteChange(seq=100, table='task_events', uid=uid, op='upsert', version=1,
                               hlc='1795480000000-0000-server', origin_gateway='fixture',
                               fields={'task_id': source['task_id'], 'kind': 'scheduled',
                                       'payload': raw, 'created_at': source['created_at'] * 1000})
    return change, raw


def test_real_import_captures_original_bytes_without_network_and_replay_is_stable(live, monkeypatch):
    def network(*args, **kwargs):
        pytest.fail('source capture attempted network')
    monkeypatch.setattr(socket, 'create_connection', network)
    change, raw = imported_source(live)
    sync.apply_remote_changes(live, [change], new_cursor=100, observers=False)
    saved = dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone())
    assert saved['event_uid'] == change.uid
    assert saved['source_json'] == raw
    assert saved['state'] == 'pending'
    sync.apply_remote_changes(live, [change], new_cursor=101, observers=False)
    assert dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone()) == saved


@pytest.mark.parametrize('mode', ['shadow', 'off'])
def test_nonprimary_import_cannot_capture(live, mode):
    change, _ = imported_source(live)
    sync.set_mode(live, mode)
    if mode == 'off':
        with pytest.raises(RuntimeError, match='not enabled'):
            sync.apply_remote_changes(live, [change], new_cursor=100, observers=False)
    else:
        sync.apply_remote_changes(live, [change], new_cursor=100, observers=False)
    assert not live.execute('SELECT 1 FROM kanban_actionability_intents').fetchone()


@pytest.mark.parametrize('deadline', [{}, [], True, 1.5, 'soon'])
def test_malformed_import_is_retained_invalid_not_a_replication_failure(live, deadline):
    change, raw = imported_source(live, mutate=lambda p: p['actionability_intent'].update(deadline_at=deadline))
    sync.apply_remote_changes(live, [change], new_cursor=100, observers=False)
    row = dict(live.execute('SELECT * FROM kanban_actionability_intents').fetchone())
    assert row['source_json'] == raw
    assert row['state'] == 'invalid'
    assert row['deadline_at'] is None
    assert row['reason'] == 'creation_intent_invalid'
    assert live.execute('SELECT payload FROM task_events WHERE uid=?', (change.uid,)).fetchone()[0] == raw


def test_import_journal_failure_rolls_back_page_and_cursor(live, monkeypatch):
    from hermes_cli import kanban_actionability_intents as intents
    change, _ = imported_source(live)
    before = sync.sync_state(live)['cursor']
    def fail(*args):
        raise RuntimeError('intent capture fault')
    monkeypatch.setattr(intents, 'capture', fail)
    with pytest.raises(RuntimeError, match='intent capture fault'):
        sync.apply_remote_changes(live, [change], new_cursor=100, observers=False)
    assert sync.sync_state(live)['cursor'] == before
    assert not live.execute('SELECT 1 FROM task_events WHERE uid=?', (change.uid,)).fetchone()


def test_foreign_and_legacy_sources_never_enter_queue(live):
    from hermes_cli import kanban_actionability_intents as intents
    source = new_park(live)
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_actionability_intents')
        live.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (source['task_id'],))
        intents.capture(live, source['uid'])
    assert not live.execute('SELECT 1 FROM kanban_actionability_intents').fetchone()
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET created_on='fixture' WHERE id=?", (source['task_id'],))
        payload = json.loads(source['payload'])
        del payload['actionability_intent']
        live.execute('UPDATE task_events SET payload=? WHERE uid=?', (json.dumps(payload), source['uid']))
        intents.capture(live, source['uid'])
    assert not live.execute('SELECT 1 FROM kanban_actionability_intents').fetchone()
