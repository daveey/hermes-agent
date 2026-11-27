"""Real board membership -> outcome adapter -> canonical reducer, without lifecycle changes."""
import json
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_retained_wait_status as helpers
import hermes_state_outcome as outcome
from hermes_state_session_status import REQUIRED_SOURCES, reduce_status

kanban_home = helpers.kanban_home
board = helpers.board


def reduce(waits):
    identity = dict(gateway='example', profile='default', session_id='s1', owner_epoch='epoch')
    return reduce_status(dict(
        identity=identity, generation=1, started=True, turn=dict(generation=1, state='completed'),
        sources={name: dict(identity=identity, generation=1, revision=1, current_revision=1,
                            coverage='complete') for name in REQUIRED_SOURCES},
        work=[dict(w, id='card:' + w['id']) for w in waits]))


def stopped(conn):
    tid = helpers._retained(conn, swept=False, request=None)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET human_request=NULL,claim_expires=NULL WHERE id=?', (tid,))
        conn.execute("UPDATE task_runs SET status='completed',outcome='decision_retained',ended_at=1 WHERE task_id=?", (tid,))
    return tid


def test_real_origin_stopped_scope_is_incomplete_not_pending_execution(board):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
    asks, waits = helpers._items()
    assert asks == []
    assert waits[0]['state'] == 'retained'
    assert waits[0]['retained_stop']['reason'] == 'scope_stopped'
    status = reduce(waits)
    assert status['kind'] == 'unresolved'
    assert status['reason'] == 'linked_work_retained'
    assert status['on'][0]['id'] == 'card:' + tid
    assert status['presentation']['stripe'] != 'done'


@pytest.mark.parametrize('state', ['running', 'ready', 'scheduled', 'triage'])
def test_real_actions_remain_waits(board, state):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (state, tid))
            kb._append_event(conn, tid, 'claimed' if state == 'triage' else 'scheduled', {})
    _, waits = helpers._items()
    assert waits[0]['state'] == state
    assert reduce(waits)['kind'] == ('running' if state == 'running' else 'waiting')


@pytest.mark.parametrize('field,value', [('current_run_id', 1), ('claim_lock', 'owner'),
                                        ('claim_expires', 123)])
def test_executor_or_lease_prevents_retained_projection(board, field, value):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (value, tid))
    _, waits = helpers._items()
    assert waits[0]['state'] == 'triage'


def test_live_human_ask_preserved(board):
    with kbc.connect_closing() as conn:
        helpers._retained(conn, swept=False)
    asks, waits = helpers._items()
    assert asks and not waits


def test_orphan_running_record_is_not_a_settled_stop(board):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET status='running',ended_at=NULL WHERE task_id=?", (tid,))
    assert helpers._items()[1][0]['state'] == 'triage'


@pytest.mark.parametrize('backfill_before_stop', [True, False])
def test_replica_history_uses_origin_order_not_arrival_order(board, backfill_before_stop):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
            kb._append_event(conn, tid, 'claimed', {})
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='claimed'",
                         (99 if backfill_before_stop else 101, tid))
    assert helpers._items()[1][0]['state'] == ('retained' if backfill_before_stop else 'triage')


def test_real_refresh_preserves_stopped_evidence_and_persists_idempotently(board, tmp_path):
    from hermes_state import SessionDB

    with kbc.connect_closing() as conn:
        tid = stopped(conn)
    outcome._reset_hooks_for_tests()
    try:
        with SessionDB(db_path=tmp_path / 'status.db') as db:
            db.create_session('s1', source='desktop')
            db.append_message('s1', role='user', content='Keep the stopped work retained.')
            db.append_message('s1', role='assistant', content='The work remains stopped and incomplete.')
            first = outcome.refresh_outcome('s1', 'card_change', db=db)
            second = outcome.refresh_outcome('s1', 'card_change', db=db)
            assert first['kind'] == 'unresolved'
            assert first['status']['reason'] == 'linked_work_retained'
            assert first['status']['on'][0]['id'] == 'card:' + tid
            assert first['status']['on'][0]['retained_stop']['reason'] == 'scope_stopped'
            assert second['status']['revision'] == first['status']['revision']
            stored = json.loads(db._read_one('SELECT outcome_json FROM sessions WHERE id=?', ('s1',))[0])
            assert stored['status'] == second['status']
    finally:
        outcome._reset_hooks_for_tests()


@pytest.mark.parametrize('later', ['scope_renewed', 'scheduled', 'continued', 'unblocked', 'promoted_manual'])
def test_later_authority_or_producer_is_not_hidden(board, later):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, later, {})
    assert helpers._items()[1][0]['state'] == 'triage'


@pytest.mark.parametrize('stale', [False, True])
def test_rehomed_sql_and_owner_record_projection(board, monkeypatch, stale):
    from hermes_cli.kanban_chat_links import _read_replica_links
    from hermes_cli.kanban_replica_projection import card_rows
    from hermes_cli import kanban_session_activity as activity

    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
            conn.execute('CREATE TABLE IF NOT EXISTS task_chat_links (uid TEXT,task_id TEXT,chat_gateway TEXT, '
                         'chat_profile TEXT,chat_session TEXT,kind TEXT,released INTEGER,created_at INTEGER)')
            conn.execute("UPDATE tasks SET created_on='other-owner' WHERE id=?", (tid,))
            conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                         "VALUES ('binding',?,'origin','default','s1','created',0,1)", (tid,))
        sql_row = _read_replica_links(conn, 'origin', 'default')[0]
        assert sql_row['retained_stop']['reason'] == 'scope_stopped'
    record = dict(sql_row, id=tid, status='triage')
    _, card = card_rows(('other-owner', 'default', 'default', tid), record, sids=['s1'], closed=False,
                        synced=100, stale=stale, me='origin', origin_profile='default',
                        legacy_record=None, historical={})[0]
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity, '_remote_cards', lambda *a: [card])
    _, waits = outcome._read_card_items(['s1'])
    assert waits[0]['state'] == ('source_unknown' if stale else 'retained')
    assert reduce(waits)['kind'] != 'done'


NOW = 2_000_000_000


def aged(conn, tid, age):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='decision_retained'", (NOW - age, tid))


@pytest.mark.parametrize('age,red', [(599, False), (600, False), (601, True), (6 * 86400, True)])
def test_stopped_retained_scope_ages_red_without_becoming_a_wait_or_an_ask(board, monkeypatch, age, red):
    """t_01a1fc39f68b07ba: six days retained in triage stayed amber; every triage cause ages after 600 s."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        aged(conn, tid, age)
        before = conn.execute('SELECT status,human_request,current_run_id,claim_lock FROM tasks WHERE id=?', (tid,)).fetchone()
        events = conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
    monkeypatch.setattr(outcome.time, 'time', lambda: NOW)
    asks, waits = helpers._items()
    assert asks == []
    assert waits[0]['state'] == 'retained'
    assert waits[0]['retained_stop']['reason'] == 'scope_stopped'
    assert waits[0]['label'].startswith('Stopped, retained and incomplete')
    assert (waits[0].get('triage_overdue') is True) is red
    status = reduce(waits)
    assert (status['kind'], status['reason']) == ('unresolved', 'linked_work_retained')
    assert status['on'][0]['state'] == 'retained'
    assert status['presentation'] == (
        {'stripe': 'stuck', 'reason_codes': ['linked_work_retained', 'triage_overdue']} if red
        else {'stripe': 'attention', 'reason_codes': ['linked_work_retained']})
    with kbc.connect_closing() as conn:  # colour is not authority: no lifecycle, ask or history write
        assert conn.execute('SELECT status,human_request,current_run_id,claim_lock FROM tasks WHERE id=?',
                            (tid,)).fetchone() == before
        assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == events


def test_real_refresh_turns_the_retained_session_red_at_the_crossing_and_clears_on_exit(board, tmp_path, monkeypatch):
    from hermes_state import SessionDB

    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        aged(conn, tid, 600)
    clock = [NOW]
    monkeypatch.setattr(outcome.time, 'time', lambda: clock[0])
    outcome._reset_hooks_for_tests()
    try:
        with SessionDB(db_path=tmp_path / 'status.db') as db:
            db.create_session('s1', source='desktop')
            db.append_message('s1', role='user', content='Keep the stopped work retained.')
            db.append_message('s1', role='assistant', content='The work remains stopped and incomplete.')
            at_600 = outcome.refresh_outcome('s1', 'card_change', db=db)['status']
            assert at_600['presentation']['stripe'] == 'attention'
            clock[0] = NOW + 1  # unchanged task data: only the clock crosses
            at_601 = outcome.refresh_outcome('s1', 'card_change', db=db)['status']
            assert at_601['presentation'] == {'stripe': 'stuck', 'reason_codes': ['linked_work_retained', 'triage_overdue']}
            assert (at_601['kind'], at_601['reason']) == ('unresolved', 'linked_work_retained')
            assert at_601['on'][0]['retained_stop']['reason'] == 'scope_stopped'
            assert at_601['on'][0]['triage_overdue'] is True
            assert at_601['revision'] == at_600['revision'] + 1
            stored = json.loads(db._read_one('SELECT outcome_json FROM sessions WHERE id=?', ('s1',))[0])
            assert stored['status'] == at_601
            with kbc.connect_closing() as conn, kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
                kb._append_event(conn, tid, 'unblocked', {})
            left = outcome.refresh_outcome('s1', 'card_change', db=db)['status']
            assert left['presentation']['stripe'] != 'stuck'
            assert 'triage_overdue' not in left['presentation']['reason_codes']
    finally:
        outcome._reset_hooks_for_tests()


@pytest.mark.parametrize('extra,kind,stripe', [
    (dict(muted=True), 'muted', 'blocked'),
    (dict(asks=[{'id': 'ask'}]), 'decision', 'stuck'),
])
def test_mute_and_human_ask_keep_precedence_over_an_aged_retained_stop(extra, kind, stripe):
    identity = dict(gateway='example', profile='default', session_id='s1', owner_epoch='epoch')
    status = reduce_status(dict(
        identity=identity, generation=1, started=True, turn=dict(generation=1, state='completed'),
        sources={name: dict(identity=identity, generation=1, revision=1, current_revision=1,
                            coverage='complete') for name in REQUIRED_SOURCES},
        work=[dict(kind='card', id='card:t', state='retained', triage_overdue=True,
                   retained_stop={'reason': 'scope_stopped'})], **extra))
    assert (status['kind'], status['presentation']['stripe']) == (kind, stripe)
    assert 'triage_overdue' not in status['presentation']['reason_codes']


def test_stale_retained_replica_is_unknown_not_red(board, monkeypatch):
    from hermes_cli import kanban_session_activity as activity

    card = dict(id='t_stale', key='other/default/default/t_stale', status='triage', stale=True, title='x',
                triage_entered_at=NOW - 6 * 86400, retained_stop={'reason': 'scope_stopped'}, session='s1')
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity, '_remote_cards', lambda *a: [card])
    monkeypatch.setattr(outcome.time, 'time', lambda: NOW)
    _, waits = outcome._read_card_items(['s1'])
    assert waits[0]['state'] == 'source_unknown' and 'triage_overdue' not in waits[0]
    assert reduce(waits)['presentation']['stripe'] != 'stuck'
    card.pop('stale')
    _, waits = outcome._read_card_items(['s1'])
    assert waits[0]['state'] == 'retained' and waits[0]['triage_overdue'] is True
