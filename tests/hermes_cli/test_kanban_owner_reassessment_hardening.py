"""Append-only, exact-identity and pre-termination custody regressions."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_owner_reassessment as r
from tests.hermes_cli import test_kanban_owner_reassessment_lifecycle as lifecycle

worker = lifecycle.worker


@pytest.mark.parametrize('recursive', [0, 1])
@pytest.mark.parametrize('operation', ['update', 'delete', 'replace_event', 'replace_episode', 'replace_rowid', 'upsert'])
def test_journal_is_append_only_even_without_recursive_triggers(recursive, operation):
    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'PRAGMA recursive_triggers={recursive}')
        r.ensure_schema(conn)
        original = ('event', 'task', 'episode', '{"original":true}', 123)
        conn.execute(f'INSERT INTO {r.PENDING_TABLE} VALUES (?,?,?,?,?)', original)
        r.ensure_schema(conn)  # repeated bootstrap retains protection
        insert = f'INSERT OR REPLACE INTO {r.PENDING_TABLE} VALUES (?,?,?,?,?)'
        statements = {
            'update': (f'UPDATE {r.PENDING_TABLE} SET receipt=\'{{}}\'', ()),
            'delete': (f'DELETE FROM {r.PENDING_TABLE}', ()),
            'upsert': (f'INSERT INTO {r.PENDING_TABLE} VALUES (?,?,?,?,?) '
                       'ON CONFLICT(event_uid) DO UPDATE SET receipt=excluded.receipt', original),
            'replace_rowid': (f'INSERT OR REPLACE INTO {r.PENDING_TABLE} '
                              '(rowid,event_uid,task_id,episode_uid,receipt,created_at) VALUES (1,?,?,?,?,?)',
                              ('new-event', 'other', 'new-episode', '{}', 456)),
            'replace_event': (insert, ('event', 'other', 'new-episode', '{}', 456)),
            'replace_episode': (insert, ('new-event', 'other', 'episode', '{}', 456)),
        }
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute(*statements[operation])
        assert conn.execute(f'SELECT * FROM {r.PENDING_TABLE}').fetchall() == [original]
        conn.execute(f'INSERT INTO {r.PENDING_TABLE} VALUES (?,?,?,?,?)', ('event2', 'task', 'episode2', '{}', 124))
        assert len(r.pending(conn)) == 2


def test_same_card_other_run_evidence_is_refused(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            with kb.write_txn(conn):
                # Existing same-card event, but not emitted by this run.
                kb._append_event(conn, ctx['task_id'], 'commented', {'text': 'old evidence'}, run_id=None)
                uid = conn.execute("SELECT uid FROM task_events WHERE kind='commented' ORDER BY id DESC LIMIT 1").fetchone()[0]
            with pytest.raises(r.ReassessmentRefused, match='owner run'):
                kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                                 awaiting=lifecycle.PRODUCER, owner_reassessment=lifecycle._opt(uid, int(time.time()) + 120),
                                 owner_origin=cap)
            assert r.pending(conn) == []
            assert kb.get_task(conn, ctx['task_id']).status == 'running'
    lifecycle._turn(worker, monkeypatch, body)


@pytest.mark.parametrize('field', ['kind', 'ref', 'gateway', 'board', 'why', 'inferred', 'shared_blocker'])
def test_commit_rejects_changed_normalized_producer_and_rolls_back(worker, monkeypatch, field):
    original_commit = r.commit
    def changed(conn, prepared, **kwargs):
        event = conn.execute("SELECT id,payload FROM task_events WHERE kind='scheduled' ORDER BY id DESC LIMIT 1").fetchone()
        payload = json.loads(event[1])
        payload['awaiting'][field] = 'different'
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event[0]))
        return original_commit(conn, prepared, **kwargs)
    monkeypatch.setattr(r, 'commit', changed)
    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            with pytest.raises(r.ReassessmentRefused, match='planned reassessment'):
                kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                                 awaiting=lifecycle.PRODUCER, owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120),
                                 owner_origin=cap)
            assert kb.get_task(conn, ctx['task_id']).status == 'running'
            assert r.pending(conn) == []
            assert conn.execute("SELECT 1 FROM task_events WHERE kind='scheduled'").fetchone() is None
    lifecycle._turn(worker, monkeypatch, body)


def test_normalized_producer_preserves_board_but_not_internal_gate_diagnostics():
    exact = {'kind': 'card', 'ref': 't_0000000000000001', 'gateway': 'remote', 'board': 'alternate', 'why': 'producer'}
    assert r.producer({**exact, 'auto_card_probe': True, 'premise': 'hash', 'probe': 'hash',
                       'deploy_window': {'status': 'healthy'}, 'observed': 'diagnostic'}) == exact
    result, _ = r.plan(lifecycle._opt('018f051a-657d-7001-8001-000000000001', 150), now=100,
                       final_backstop=200, awaiting=exact, payload={'wake_at': 200})
    assert result['awaiting'] == exact
    with pytest.raises(r.ReassessmentRefused):
        r.plan(lifecycle._opt('018f051a-657d-7001-8001-000000000001', 150), now=100,
               final_backstop=200, awaiting={**exact, 'board': ''}, payload={'wake_at': 200})


def test_real_gate_normalization_is_the_receipts_exact_producer(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        declared = {'kind': ' EXTERNAL ', 'ref': ' executor:example ', 'why': ' example queue ',
                    'gateway': 'local', 'board': 'alternate'}
        with kbc.connect_closing() as conn:
            assert kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                                    awaiting=declared, owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120),
                                    owner_origin=cap)
            receipt = json.loads(r.pending(conn)[0]['receipt'])
            stored = json.loads(receipt['park_event']['payload'])['awaiting']
            assert receipt['producer'] == {'kind': 'external', 'ref': 'executor:example',
                                           'why': 'example queue', 'board': 'alternate'}
            assert receipt['producer'] == r.producer(stored)
            assert 'premise' in stored and 'premise' not in receipt['producer']
    lifecycle._turn(worker, monkeypatch, body)


def test_actual_custody_rechecked_in_park_transaction_before_termination(worker, monkeypatch):
    from agent.origin_worker_custody import CapturedWorkerOrigin
    original = CapturedWorkerOrigin.validated
    observed = []
    def body(cap, ctx, agent, evidence):
        def losing(self, conn=None):
            if conn is not None:
                state = conn.execute('SELECT status,current_run_id FROM tasks WHERE id=?', (ctx['task_id'],)).fetchone()
                observed.append((conn.in_transaction, tuple(state)))
                if len(observed) == 2:
                    return None
            return original(self, conn)
        monkeypatch.setattr(CapturedWorkerOrigin, 'validated', losing)
        with kbc.connect_closing() as conn:
            with pytest.raises(r.ReassessmentRefused):
                kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                                 awaiting=lifecycle.PRODUCER, owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120),
                                 owner_origin=cap)
            assert kb.get_task(conn, ctx['task_id']).status == 'running'
            assert r.pending(conn) == []
    lifecycle._turn(worker, monkeypatch, body)
    assert len(observed) == 2
    assert all(txn and state == ('running', worker[3]['run_id']) for txn, state in observed)


def test_schedule_emits_portable_source_fixture(worker, monkeypatch, tmp_path, record_property):
    """Export only real API-generated rows, never reconstruct a receipt by hand."""
    def body(cap, ctx, agent, evidence):
        from tools import kanban_tools
        monkeypatch.setenv('HERMES_KANBAN_TASK', ctx['task_id'])
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(ctx['run_id']))
        reply = json.loads(kanban_tools._handle_schedule({
            'reason': lifecycle._until(), 'awaiting': lifecycle.PRODUCER,
            'owner_reassessment': lifecycle._opt(evidence, int(time.time()) + 120)}))
        assert reply.get('ok') is True, reply
        with kbc.connect_closing() as conn:
            journal, = r.pending(conn, ctx['task_id'])
            tables = ['tasks', 'task_runs', 'task_events', 'kanban_sync_outbox']
            rows = {}
            for table in tables:
                columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
                assert columns, table
                rows[table] = [dict(zip(columns, row)) for row in conn.execute(f'SELECT * FROM {table}')]
            fixture = {'reply': reply, 'receipt': json.loads(journal['receipt']), 'pending': journal, 'source_rows': rows}
            assert rows['kanban_sync_outbox']
            fixture_path = tmp_path / 'core-emitted-fixture.json'
            fixture_path.write_text(json.dumps(fixture, indent=2), encoding='utf-8')
            loaded = json.loads(fixture_path.read_text(encoding='utf-8'))
            assert loaded['receipt']['park_event']['payload'] == next(
                event['payload'] for event in rows['task_events'] if event['uid'] == journal['event_uid'])
            record_property('core_emitted_fixture', json.dumps(loaded))
    lifecycle._turn(worker, monkeypatch, body)
