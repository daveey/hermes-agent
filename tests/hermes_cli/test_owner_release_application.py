"""APPLIED proof is separate from consumed; no live HOME admission is tested here."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_release_certificate as cert, kanban_release_replica as replica
from tests.hermes_cli import test_owner_release_certificate as helpers

proof = helpers.proof
store = helpers.store


def setup(conn, receipt):
    from hermes_cli import kanban_release_application as applied
    conn.row_factory = sqlite3.Row
    conn.execute("ALTER TABLE kanban_sync_state ADD COLUMN gateway_id TEXT DEFAULT 'home'")
    conn.execute("ALTER TABLE kanban_sync_state ADD COLUMN mode TEXT DEFAULT 'primary'")
    conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,task_id TEXT,kind TEXT,payload TEXT,created_at INTEGER,origin_gateway TEXT,run_id INTEGER)")
    replica._store(conn, receipt)
    applied.install(conn)
    from hermes_cli import kanban_release_home_queue as queue, kanban_release_intent as intent
    queue.install(conn)
    carrier_payload = {'version': 1, 'carrier': 'owner_release', 'action': 'owner_release', 'request_uid': 'delivery-1',
                       'board': 'example', 'target_task_id': 't_target', 'home': 'home', 'intent': receipt['intent'],
                       'digest': receipt['digest'], 'seen_comments': [], 'nonce': 'a' * 64}
    raw = cert.encode(carrier_payload)
    wire = cert.encode(receipt['consume'])
    conn.execute(f'INSERT INTO {queue.TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)',
                 ('delivery-1', 'example', 't_target', raw, 99, 'intent-1', 'consumed_pending_apply',
                  wire, intent.digest(receipt['consume']), 0))
    add(conn, {**event_row('delivery-1', 'operator_call_requested', carrier_payload),
               'payload': raw, 'created_at': 99, 'origin_gateway': 'origin'})
    event = receipt['renewal']
    add(conn, {k: event[k] for k in applied.EVENT_FIELDS})
    payload = {'version': 1, 'intent_uid': 'intent-1', 'consume_uid': 'consume-1', 'delivery_uid': 'delivery-1',
               'outcome': 'released', 'mandate': 'Released original goal; original agent authority only.',
               'effect_uids': ['release-current', 'release-old', 'budget-1', 'promote-1']}
    actor = receipt['intent']['actor']
    base = {'version': 1, 'owner_release_intent_uid': 'intent-1', 'consume_uid': 'consume-1',
            'actor_kind': 'agent', 'actor_gateway': actor['gateway'], 'actor_profile': actor['profile'],
            'actor_session': actor['session'], 'authority_kind': 'chat'}
    effects = [event_row('release-current', 'stop_released', {**base, 'episode': 'stop-1'}),
               event_row('release-old', 'stop_released', {**base, 'episode': 'older-1'}),
               event_row('budget-1', 'spend_budget_set', {'runs': 5, 'writer_kind': 'agent'}),
               event_row('promote-1', 'promoted_manual', {'from_status': 'triage'})]
    for effect in effects:
        add(conn, effect)
    result = event_row('result-1', 'owner_release_applied', payload)
    add(conn, result)
    return result


def event_row(uid, kind, payload):
    return dict(uid=uid, task_id='t_target', kind=kind, payload=json.dumps(payload), created_at=101, origin_gateway='home')


def add(conn, row):
    cols = list(row)
    conn.execute(f"INSERT INTO task_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row.values()))


def record(conn, result):
    from hermes_cli import kanban_release_application as applied
    with applied._writer(conn):
        applied._record(conn, consume_uid='consume-1', delivery_uid='delivery-1', result_uid=result['uid'])


def test_consumed_only_is_not_applied(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    replica._store(conn, proof)
    assert applied.verified_application(helpers.Rows(conn), 'intent-1') is None
    assert applied.verified_renewal(helpers.Rows(conn), proof['renewal']) is None


def test_applied_reopen_exact_intent_context_no_fake_comment(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, path = store
    result = setup(conn, proof)
    record(conn, result)
    before = conn.execute(f'SELECT * FROM {applied.TABLE}').fetchall()
    record(conn, result)
    assert conn.execute(f'SELECT * FROM {applied.TABLE}').fetchall() == before
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as ro:
        ro.execute('PRAGMA query_only=ON')
        found = applied.verified_application(helpers.Rows(ro), 'intent-1')
        assert found['intent'] == proof['intent']
        renewal = applied.verified_renewal(helpers.Rows(ro), proof['renewal'])
        assert renewal['mandate']['kind'] == 'owner_release_intent'
        assert renewal['mandate']['body'] == json.loads(result['payload'])['mandate']
        assert 'mandate_comment_uid' not in renewal['payload']
        assert set(renewal['payload']['covered_restriction_uids']) == cert.coverage(proof)


def test_real_rollback_after_record_is_not_applied(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    with pytest.raises(RuntimeError, match='later refusal'):
        with applied._writer(conn):
            applied._record(conn, consume_uid='consume-1', delivery_uid='delivery-1', result_uid=result['uid'])
            conn.execute("INSERT INTO task_events(uid,task_id,kind) VALUES ('later','t_target','ordinary')")
            raise RuntimeError('later refusal')
    assert applied.verified_application(helpers.Rows(conn), 'intent-1') is None
    assert conn.execute("SELECT 1 FROM task_events WHERE uid='later'").fetchone() is None


def test_record_requires_own_canonical_writer_not_arbitrary_transaction(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(ValueError, match='application writer'):
            applied._record(conn, consume_uid='consume-1', delivery_uid='delivery-1', result_uid=result['uid'])
    finally:
        conn.rollback()
    assert applied.verified_application(conn, 'intent-1') is None


@pytest.mark.parametrize('statement', [
    'DELETE FROM {table}', "UPDATE {table} SET evidence='{{}}'",
    'INSERT OR REPLACE INTO {table} SELECT * FROM {table}',
])
def test_receipt_is_immutable_even_with_permit(proof, store, statement):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    with pytest.raises(sqlite3.DatabaseError):
        with applied._writer(conn):
            conn.execute(statement.format(table=applied.TABLE))
    assert applied.verified_application(conn, 'intent-1') is not None


@pytest.mark.parametrize('uid', ['consume-1','delivery-1','result-1','release-current','release-old','budget-1','promote-1'])
def test_changed_or_missing_event_half_revokes_applied(proof, store, uid):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    original = conn.execute('SELECT payload FROM task_events WHERE uid=?', (uid,)).fetchone()[0]
    conn.execute("UPDATE task_events SET payload='{}' WHERE uid=?", (uid,))
    assert applied.verified_application(conn, 'intent-1') is None
    conn.execute('UPDATE task_events SET payload=? WHERE uid=?', (original, uid))
    assert applied.verified_application(conn, 'intent-1') is not None
    conn.execute('DELETE FROM task_events WHERE uid=?', (uid,))
    assert applied.verified_application(conn, 'intent-1') is None


@pytest.mark.parametrize('mutate', ['actor','history','extra_history','result_actor','outcome','mandate','distinct','run'])
def test_record_refuses_wrong_binding_before_receipt(proof, store, mutate):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    target = 'release-old' if mutate in ('actor','history','extra_history') else 'result-1'
    p = json.loads(conn.execute('SELECT payload FROM task_events WHERE uid=?', (target,)).fetchone()[0])
    updates = {
        'actor': {'actor_gateway': 'forged'}, 'history': {'episode': 'stop-1'},
        'extra_history': {'episode': 'unselected'}, 'result_actor': {'delivery_uid': 'another'},
        'outcome': {'outcome': 'consumed_pending_apply'}, 'mandate': {'mandate': ''},
        'distinct': {'effect_uids': [*p.get('effect_uids', []), 'release-old']}, 'run': {},
    }
    p.update(updates[mutate])
    if mutate == 'run':
        conn.execute("UPDATE task_events SET run_id=1 WHERE uid='result-1'")
    conn.execute('UPDATE task_events SET payload=? WHERE uid=?', (json.dumps(p), target))
    with pytest.raises(ValueError):
        record(conn, result)
    assert applied.verified_application(conn, 'intent-1') is None


def test_foreign_board_and_forged_renewal_failclosed(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    altered = copy.deepcopy(proof['renewal'])
    altered['origin_gateway'] = 'other'
    assert applied.verified_renewal(conn, altered) is None
    assert applied.verified_application(conn, 'intent-1') is not None
    conn.execute("UPDATE kanban_sync_state SET board_id='other'")
    assert applied.verified_application(conn, 'intent-1') is None


@pytest.mark.parametrize('table', ['applied', 'sync', 'events', 'queue'])
def test_missing_or_malformed_optional_schema_is_no_coverage(proof, store, table):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    name = {'applied': applied.TABLE, 'sync': 'kanban_sync_state', 'events': 'task_events',
            'queue': 'owner_release_home_queue'}[table]
    conn.execute(f'ALTER TABLE {name} RENAME TO broken_fixture_source')
    conn.execute(f'CREATE TABLE {name}(irrelevant TEXT)')
    assert applied.verified_application(helpers.Rows(conn), 'intent-1') is None
    assert applied.verified_renewal(helpers.Rows(conn), proof['renewal']) is None


@pytest.mark.parametrize('mutation', ['stage', 'wire', 'missing'])
def test_delivery_custody_cannot_be_invented_or_swapped(proof, store, mutation):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    # Deliberately damaged historical custody: ordinary production trigger stays
    # separately tested in test_owner_release_home_recovery.py.
    conn.execute('DROP TRIGGER owner_release_home_queue_update')
    conn.execute('DROP TRIGGER owner_release_home_queue_delete')
    if mutation == 'stage':
        conn.execute("UPDATE owner_release_home_queue SET stage='consume_pending'")
    elif mutation == 'wire':
        p = copy.deepcopy(proof['consume'])
        p['client_op_id'] = 'another-consume'
        from hermes_cli import kanban_release_intent as intent
        conn.execute('UPDATE owner_release_home_queue SET consume_wire=?,wire_digest=?', (cert.encode(p), intent.digest(p)))
    else:
        conn.execute('DELETE FROM owner_release_home_queue')
    with pytest.raises(ValueError):
        record(conn, result)
    assert applied.verified_application(conn, 'intent-1') is None


@pytest.mark.parametrize('field', ['delivery_uid', 'result_uid'])
def test_existing_receipt_replay_requires_exact_requested_ids(proof, store, field):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    args = {'consume_uid': 'consume-1', 'delivery_uid': 'delivery-1', 'result_uid': result['uid']}
    args[field] = 'other-id'
    with pytest.raises(ValueError, match='replay identity'):
        with applied._writer(conn):
            applied._record(conn, **args)
    assert applied.verified_application(conn, 'intent-1') is not None


@pytest.mark.parametrize('origin', ['other', None])
def test_echo_only_promotes_private_captured_local_origin(proof, store, origin):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    local_ids = ('release-current', 'release-old', 'budget-1', 'promote-1', 'result-1')
    conn.execute("UPDATE task_events SET origin_gateway=NULL WHERE kind NOT IN ('scope_renewed','operator_call_requested')")
    record(conn, result)
    for uid in local_ids:
        conn.execute("UPDATE task_events SET origin_gateway='home' WHERE uid=?", (uid,))
    assert applied.verified_application(conn, 'intent-1') is not None
    record(conn, result)  # same frozen result, not a second receipt on echo
    conn.execute("UPDATE task_events SET origin_gateway=? WHERE uid='result-1'", (origin,))
    # A null frozen origin may still be local; a foreign stamped origin never is.
    assert (applied.verified_application(conn, 'intent-1') is not None) == (origin is None)
    conn.execute("UPDATE task_events SET origin_gateway=NULL WHERE uid='consume-1'")
    assert applied.verified_application(conn, 'intent-1') is None


@pytest.mark.parametrize('column,value', [('gateway_id', 'origin'), ('mode', 'shadow')])
def test_first_application_requires_exact_primary_home(proof, store, column, value):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    conn.execute(f'UPDATE kanban_sync_state SET {column}=?', (value,))
    with pytest.raises(ValueError, match='primary HOME'):
        record(conn, result)
    assert applied.verified_application(conn, 'intent-1') is None


def test_nested_writer_refused_and_permit_does_not_escape(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(RuntimeError, match='nested'):
            with applied._writer(conn):
                raise AssertionError('nested reservation admitted')
        assert conn.in_transaction
    finally:
        conn.rollback()
    with pytest.raises(RuntimeError, match='rollback'):
        with applied._writer(conn):
            raise RuntimeError('rollback')
    assert applied._PERMIT.get() is None
    with pytest.raises(ValueError, match='application writer'):
        applied._record(conn, consume_uid='consume-1', delivery_uid='delivery-1', result_uid=result['uid'])


def test_actual_receipt_insert_failure_rolls_back_prior_writes(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    conn.execute(f"CREATE TRIGGER fail_application AFTER INSERT ON {applied.TABLE} BEGIN SELECT RAISE(ABORT,'fixture insertion failure'); END")
    before = [tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')]
    with pytest.raises(sqlite3.DatabaseError, match='fixture insertion failure'):
        with applied._writer(conn):
            conn.execute("INSERT INTO task_events(uid,task_id,kind) VALUES ('prior','t_target','ordinary')")
            applied._record(conn, consume_uid='consume-1', delivery_uid='delivery-1', result_uid=result['uid'])
    assert [tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')] == before
    assert applied.verified_application(conn, 'intent-1') is None


def test_complete_envelope_bound_is_independent_of_each_event(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    # Each event stays beneath the per-event cap, but all frozen effects together
    # exceed the envelope cap. No partial receipt is retained.
    for uid in ('budget-1', 'promote-1'):
        p = {'retained': 'x' * (cert.MAX_BYTES // 2 + 100)}
        conn.execute('UPDATE task_events SET payload=? WHERE uid=?', (json.dumps(p), uid))
    with pytest.raises(ValueError, match='oversized'):
        record(conn, result)
    assert applied.verified_application(conn, 'intent-1') is None


def test_direct_insert_and_rowid_replace_refused_inside_writer(proof, store):
    from hermes_cli import kanban_release_application as applied
    conn, _ = store
    result = setup(conn, proof)
    record(conn, result)
    row = tuple(conn.execute(f'SELECT * FROM {applied.TABLE}').fetchone())
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f'INSERT INTO {applied.TABLE} VALUES (?,?,?,?,?,?,?)',
                     ('new-intent', 'new-consume', 'new-delivery', *row[3:]))
    with pytest.raises(sqlite3.DatabaseError):
        with applied._writer(conn):
            conn.execute(f'INSERT OR REPLACE INTO {applied.TABLE}(rowid,intent_uid,consume_uid,delivery_uid,board,task_id,evidence,digest) '
                         'VALUES (?,?,?,?,?,?,?,?)', (1, 'new-intent', 'new-consume', 'new-delivery', *row[3:]))
    assert applied.verified_application(conn, 'intent-1') is not None

