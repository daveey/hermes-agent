"""Cross-connection and UID-indirection controls for home-custody maintenance."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_sync_attachments as attachments, kanban_spend_caused_review as caused
from hermes_cli import kanban_spend_review_request as journal, kanban_sync_client as client
from tests.hermes_cli import test_home_shared_consumer_custody as support

consumer_board = support.consumer_board


def seed_attachments(c):
    data = b'fixture bytes'
    key = 'sha256:' + hashlib.sha256(data).hexdigest()
    for tid in ('t_A', 't_B'):
        support.card(c, tid, 'todo')
        with kb.write_txn(c):
            c.execute("INSERT INTO task_attachments(task_id,uid,filename,stored_path,size,created_at,blob_key) "
                      "VALUES (?,?,?,'',?,1,?)", (tid, 'attachment_' + tid, 'probe.txt', len(data), key))
    return data


@pytest.mark.parametrize('change', ['pending', 'rebound', 'deleted'])
def test_network_callback_real_other_connection_cannot_abort_peer_or_stamp_rebound(consumer_board, change):
    c = consumer_board
    data = seed_attachments(c)
    assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    path = c.execute('PRAGMA database_list').fetchone()[2]
    calls = []
    with closing(dbc.connect(Path(path))) as other:
        class Hub:
            def put_attachment(self, *a): raise AssertionError('no upload')
            def get_attachment_blob(self, board, uid):
                calls.append(uid)
                if uid == 'attachment_t_A':
                    if change == 'rebound':
                        support.card(other, 't_new', 'todo')
                    with kb.write_txn(other):
                        if change == 'rebound':
                            other.execute("UPDATE task_attachments SET task_id='t_new' WHERE uid=?", (uid,))
                        elif change == 'deleted':
                            other.execute('DELETE FROM task_attachments WHERE uid=?', (uid,))
                    if change != 'deleted':
                        support.freeze(other, 't_new' if change == 'rebound' else 't_A', 'pending')
                return data
        assert attachments.transfer_pending(c, Hub(), 'default', limit=2) == 1
    assert calls == ['attachment_t_A', 'attachment_t_B']
    assert c.execute("SELECT stored_path FROM task_attachments WHERE task_id='t_B'").fetchone()[0]
    assert c.execute("SELECT 1 FROM kanban_attachment_retry WHERE uid='attachment_t_A'").fetchone() is None


def test_publication_holds_real_writer_reservation(consumer_board, monkeypatch):
    c = consumer_board
    data = seed_attachments(c)
    path = c.execute('PRAGMA database_list').fetchone()[2]
    published = []
    original = attachments._publish_file
    with closing(dbc.connect(Path(path))) as other:
        other.execute('PRAGMA busy_timeout=0')
        def publish(destination, payload):
            assert c.in_transaction
            # A distinct SQLite writer cannot prepare between the check and replacement.
            import sqlite3
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            published.append(str(destination))
            original(destination, payload)
        monkeypatch.setattr(attachments, '_publish_file', publish)
        class Hub:
            def put_attachment(self, *a): raise AssertionError('no upload')
            def get_attachment_blob(self, *a): return data
        assert attachments.transfer_pending(c, Hub(), 'default') == 2
    assert len(published) == 2


@pytest.mark.parametrize('with_epoch', [False, True])
def test_epoch_envelope_keeps_semantic_receipt_digest_and_exact_replay(consumer_board, monkeypatch, with_epoch):
    """Board spend_review WIRE/hash contract is semantic; epochs are ingress sidecars.

    Source read: hermes-hub 6c67e851 worker/src/spend_review.ts WIRE and prepareSpendReview.
    This is a protocol-adapter fixture, not a claim of deployed Board interoperability.
    """
    c = consumer_board
    ep, _wire = support.seed_request(c)
    if with_epoch:
        support.freeze(c, 't_A', 'completed')
    sent = []
    class Hub:
        ident = SimpleNamespace(gateway='worker-a3')
        def decision_retain_capability(self, board): return {'spend_review': 1}
        def ops(self, board, wires):
            sent.extend(wires)
            return {'results': [{'client_op_id': wires[0]['event_uid'], 'status': 'applied'}]}
        def row_evidence(self, board, table, uid):
            # Adapter strips the envelope before semantic prepare/hash, matching other typed intents.
            op = {k: v for k, v in sent[0].items() if k != 'home_generations'}
            payload = {k: op[k] for k in ('task', 'episode', 'verdict', 'reason', 'max_grants')}
            payload.update(version=1, runs=None, quote=None, target=None, request_uid=uid,
                request_sha256=hashlib.sha256(json.dumps(op, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=False).encode()).hexdigest(), actor_kind='system', actor_session=None,
                actor_gateway='worker-a3', authority_kind='spend_reviewer', authority_ref='kanban-spend-reviewer')
            row = dict(uid=uid, task_id=op['task'], kind='spend_review', version=1,
                       origin_gateway='worker-a3', deleted_at=None, payload=json.dumps(payload))
            return dict(board=board, table=table, uid=uid, state='live', row=row)
        def close(self): pass
    monkeypatch.setattr(client, 'Hub', lambda *a, **kw: Hub())
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'push', lambda *a, **kw: None)
    fields = {'verdict': 'escalate', 'reason': 'bounded decision'}
    assert caused.canonical(c, 't_A', ep, fields, 2)['verdict'] == 'escalate'
    stored = journal.get(c, 't_A', ep)
    assert 'home_generations' not in stored['wire']
    assert sent[0].get('home_generations') == ({'t_A': 'fixture-generation-0001'} if with_epoch else None)
    support.freeze(c, 't_A', 'completed')
    assert caused.canonical(c, 't_A', ep, fields, 2)['verdict'] == 'escalate'
    assert len(sent) == 1 and journal.get(c, 't_A', ep) == stored


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'completed'])
def test_review_target_is_captured_not_just_source(consumer_board, phase):
    c = consumer_board
    ep, wire = support.seed_request(c)
    support.card(c, 't_target', 'todo')
    support.freeze(c, 't_target', phase)
    wire = {**wire, 'verdict': 'wait_on', 'target': 't_target'}
    if phase != 'completed':
        with pytest.raises(ValueError, match='home transfer'):
            journal.freeze(c, 'board', 'worker-a3', wire)
        assert journal.get(c, 't_A', ep) is None
    else:
        request = journal.freeze(c, 'board', 'worker-a3', wire)
        assert json.loads(request['home_generations']) == {'t_target': 'fixture-generation-0001'}

