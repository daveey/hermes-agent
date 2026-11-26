"""Real HOME assembly trial; only a frozen proposal survives, never clearance."""
from __future__ import annotations

import copy
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_owner_release_assemble as helpers

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly


def prepare(a):
    from hermes_cli import kanban_release_prepared as subject
    return subject.prepare(a.conn, a.proof, delivery_uid='delivery-1')


def snapshot(a):
    return {name: [tuple(r) for r in a.conn.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in ('tasks', 'task_comments', 'task_events', 'kanban_sync_outbox',
                         'owner_release_applied_receipts', 'kanban_sync_state')}


def test_actual_trial_rolls_back_every_effect_but_freezes_complete_proposal(assembly):
    from hermes_cli import kanban_release_application as app, kanban_spend_decision as decision
    a = assembly
    before = snapshot(a)
    prepared = prepare(a)
    assert snapshot(a) == before
    assert prepared['task_before']['status'] == 'triage'
    assert prepared['task_after']['status'] == 'ready'
    assert prepared['application']['result']['outcome'] == 'released'
    assert len(prepared['comments']) == 1
    comment = prepared['comments'][0]
    assert comment['uid'] and comment['author'] == 'kanban-spend-budget'
    kinds = [e['kind'] for e in prepared['events']]
    assert kinds == ['wake_episode_evidence', 'commented', 'spend_budget_set', 'stop_released', 'stop_released', 'promoted_manual', 'owner_release_applied']
    assert all(e['uid'] for e in prepared['events'])
    assert app.verified_application(a.conn, 'intent-1') is None
    assert decision.execution_restricted(a.conn, a.tid)
    assert kb.promote_task(a.conn, a.tid, actor='unqualified')[0] is False
    assert kb.get_task(a.conn, a.tid).status == 'triage'


def test_historical_trial_replay_before_changed_owner_and_new_episode(assembly):
    a = assembly
    expected = prepare(a)
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_chat_links SET released=1 WHERE uid='link-1'")
        kb._append_event(a.conn, a.tid, 'spend_budget_triaged', {'cause': 'scope_stopped'})
    before = snapshot(a)
    assert prepare(a) == expected
    assert snapshot(a) == before


def test_proposal_replay_binds_exact_proof_and_delivery(assembly):
    from hermes_cli import kanban_release_prepared as subject
    a = assembly
    prepare(a)
    other = copy.deepcopy(a.proof)
    other['intent']['note'] = 'different'
    with pytest.raises(ValueError, match='replay'):
        subject.prepare(a.conn, other, delivery_uid='delivery-1')
    with pytest.raises(ValueError, match='replay'):
        subject.prepare(a.conn, a.proof, delivery_uid='other-delivery')


def test_trial_late_veto_preserves_existing_outbox_and_all_source_rows(assembly, monkeypatch):
    from hermes_cli import kanban_release_assemble as assemble
    a = assembly
    before = snapshot(a)
    def reject(_conn, _tid):
        raise ValueError('late trial veto')
    monkeypatch.setattr(assemble, '_private_veto', reject)
    with pytest.raises(ValueError, match='late trial veto'):
        prepare(a)
    assert snapshot(a) == before


def test_prepared_insert_failure_rolls_back_trial_and_proposal(assembly):
    from hermes_cli import kanban_release_prepared as subject
    a = assembly
    subject.install(a.conn)
    a.conn.execute(f"CREATE TRIGGER fail_proposal AFTER INSERT ON {subject.TABLE} BEGIN SELECT RAISE(ABORT,'proposal failure'); END")
    before = snapshot(a)
    with pytest.raises(sqlite3.DatabaseError, match='proposal failure'):
        prepare(a)
    assert snapshot(a) == before
    assert subject.verified_prepared(a.conn, 'intent-1') is None


@pytest.mark.parametrize('mode', ['update', 'delete', 'replace', 'rowid', 'outside_writer'])
def test_proposal_is_append_only_even_under_actual_application_writer(assembly, mode):
    from hermes_cli import kanban_release_prepared as subject, kanban_release_application as app
    a = assembly
    prepare(a)
    row = a.conn.execute(f'SELECT rowid,* FROM {subject.TABLE}').fetchone()
    statements = {
        'update': (f"UPDATE {subject.TABLE} SET digest='other'", ()),
        'delete': (f'DELETE FROM {subject.TABLE}', ()),
        'replace': (f'INSERT OR REPLACE INTO {subject.TABLE} SELECT * FROM {subject.TABLE}', ()),
        'rowid': (f'INSERT OR REPLACE INTO {subject.TABLE}(rowid,intent_uid,consume_uid,delivery_uid,board,task_id,evidence,digest) VALUES (?,?,?,?,?,?,?,?)',
                  (row[0], 'other-intent', 'other-consume', 'other-delivery', *tuple(row)[4:])),
        'outside_writer': (f'INSERT INTO {subject.TABLE} VALUES (?,?,?,?,?,?,?)', ('other', 'other-consume', 'other-delivery', 'example', a.tid, '{}', '0'*64)),
    }
    sql, args = statements[mode]
    writer = kb.write_txn if mode == 'outside_writer' else app._writer
    with pytest.raises(sqlite3.DatabaseError, match='immutable'):
        with writer(a.conn):
            a.conn.execute(sql, args)
    assert subject.verified_prepared(a.conn, 'intent-1') is not None


@pytest.mark.parametrize('mode', ['sync_off', 'missing_capture', 'wrong_home'])
def test_trial_refuses_missing_real_uid_capture_or_wrong_primary(assembly, mode):
    from hermes_cli import kanban_db_sync as sync
    a = assembly
    if mode == 'missing_capture':
        sync.drop_triggers(a.conn)
    elif mode == 'sync_off':
        a.conn.execute("UPDATE kanban_sync_state SET mode='off'")
    else:
        a.conn.execute("UPDATE kanban_sync_state SET gateway_id='foreign'")
    a.conn.commit()
    before = snapshot(a)
    with pytest.raises(ValueError):
        prepare(a)
    assert snapshot(a) == before


@pytest.mark.parametrize('assembly', [{'runs': 3}], indirect=True)
def test_unlimited_budget_trial_does_not_fabricate_budget_comment(assembly):
    a = assembly
    with kb.write_txn(a.conn):
        kb._append_event(a.conn, a.tid, 'spend_budget_set',
                         {'runs': 0, 'cache_read_tokens': 0, 'writer_kind': 'agent', 'by': 'prior', 'reason': 'existing unlimited'})
    before = snapshot(a)
    result = prepare(a)
    assert snapshot(a) == before
    assert result['comments'] == []
    assert 'spend_budget_set' not in [e['kind'] for e in result['events']]


def test_sql_only_read_after_real_reopen_does_not_need_proposed_rows(assembly):
    from hermes_cli import kanban_release_prepared as subject
    from tests.hermes_cli import test_owner_release_certificate as cert_helpers
    a = assembly
    expected = prepare(a)
    path = a.conn.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as reader:
        assert subject.verified_prepared(cert_helpers.Rows(reader), 'intent-1') == expected


@pytest.mark.parametrize('damage', ['digest', 'intent', 'consume', 'delivery', 'task', 'budget_comment', 'missing_effect', 'result', 'delta', 'duplicate', 'shape'])
def test_corrupt_proposal_never_backfills_or_changes_source(assembly, damage):
    from hermes_cli import kanban_release_prepared as subject, kanban_release_application as app
    from hermes_cli import kanban_release_certificate as cert
    a = assembly
    data = prepare(a)
    changes = {
        'intent': lambda: data['consumption']['intent'].update(note='changed'),
        'consume': lambda: data['consumption']['consume'].update(client_op_id='other'),
        'delivery': lambda: data.update(delivery_uid='other'),
        'task': lambda: data['task_before'].update(id='other'),
        'budget_comment': lambda: data['comments'][0].update(uid='other'),
        'missing_effect': lambda: data['events'].pop(),
        'result': lambda: data['application']['result'].update(mandate='changed'),
        'delta': lambda: data['task_after'].update(title='changed'),
        'duplicate': lambda: data['events'].append(data['events'][0]),
        'shape': lambda: data.update(version=True),
    }
    if damage != 'digest':
        changes[damage]()
    # Fixture-only bypass to exercise defensive reads, not permission to mutate.
    a.conn.execute(f'DROP TRIGGER {subject.TABLE}_update')
    with kb.write_txn(a.conn):
        a.conn.execute(f'UPDATE {subject.TABLE} SET evidence=?,digest=?',
                       (cert.encode(data), '0'*64 if damage == 'digest' else app._sha(data)))
    before = snapshot(a)
    assert subject.verified_prepared(a.conn, 'intent-1') is None
    with pytest.raises(ValueError, match='custody is corrupt'):
        prepare(a)
    assert snapshot(a) == before


def test_observed_trial_cannot_transfer_to_another_writer_invocation(assembly):
    from hermes_cli import kanban_release_prepared as subject, kanban_release_application as app
    a = assembly
    subject.install(a.conn)
    with app._writer(a.conn):
        trial = subject._observe(a.conn, a.proof, 'delivery-1')
    with app._writer(a.conn), pytest.raises(ValueError, match='bound to this writer'):
        subject._store(a.conn, trial)


def test_observe_requires_actual_application_writer(assembly):
    from hermes_cli import kanban_release_prepared as subject
    with kb.write_txn(assembly.conn), pytest.raises(ValueError, match='unique application writer'):
        subject._observe(assembly.conn, assembly.proof, 'delivery-1')


def test_capture_error_after_real_assembly_rolls_back_even_before_proposal(assembly, monkeypatch):
    from hermes_cli import kanban_release_prepared as subject
    a = assembly
    before = snapshot(a)
    original = subject._validate
    def reject(data):
        original(data)
        raise ValueError('capture failed after actual effects')
    monkeypatch.setattr(subject, '_validate', reject)
    with pytest.raises(ValueError, match='capture failed'):
        prepare(a)
    assert snapshot(a) == before


@pytest.mark.parametrize('damage', ['list-decision', 'null-decision', 'string-decision', 'reordered-effects', 'substituted-effect', 'omitted-effect'])
def test_coherent_nested_corruption_is_not_a_valid_proposal(assembly, damage):
    from hermes_cli import kanban_release_prepared as subject, kanban_release_application as app
    from hermes_cli import kanban_release_certificate as cert
    a = assembly
    data = prepare(a)
    application = data['application']
    if damage.endswith('decision'):
        bad = {'list-decision': [], 'null-decision': None, 'string-decision': 'bad'}[damage]
        effect = next(e for e in application['effects'] if e['kind'] == 'spend_budget_set')
        payload = cert.parse(effect['payload'])
        payload['decision_comment'] = bad
        effect['payload'] = cert.encode(payload)
        next(e for e in data['events'] if e['uid'] == effect['uid'])['payload'] = effect['payload']
    else:
        refs = application['result']['effect_uids']
        if damage == 'reordered-effects':
            refs.reverse()
        elif damage == 'substituted-effect':
            refs[0] = 'never-generated-effect'
        else:
            refs.pop()
        application['result_event']['payload'] = cert.encode(application['result'])
        next(e for e in data['events'] if e['uid'] == application['result_event']['uid'])['payload'] = cert.encode(application['result'])
    a.conn.execute(f'DROP TRIGGER {subject.TABLE}_update')
    with kb.write_txn(a.conn):
        a.conn.execute(f'UPDATE {subject.TABLE} SET evidence=?,digest=?', (cert.encode(data), app._sha(data)))
    before = snapshot(a)
    assert subject.verified_prepared(a.conn, 'intent-1') is None
    with pytest.raises(ValueError, match='custody is corrupt'):
        prepare(a)
    assert snapshot(a) == before
