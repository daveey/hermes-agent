"""Authenticated evidence transport and fully scoped finalization custody."""
import copy
import sqlite3
import json
from dataclasses import asdict
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_transfer as transfer, kanban_containment_replica as replica
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_home_source_stop as support
from tests.hermes_cli import test_kanban_containment_replica as custody
from tests.hermes_cli.test_review_home_feed_isolation import change
board = support.board

@pytest.mark.parametrize('prepared', [False, True])
def test_uncached_containment_native_pull_keeps_neighbor_progress(board, monkeypatch, prepared):
    target, _, stop, _, hub = custody.scenario(board)
    sync.set_identity(board, 'fixture', 'worker-a3')
    replica.install(board)
    peer = kb.create_task(board, title='healthy')
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (target,))
        board.execute('DELETE FROM kanban_sync_outbox')
        if prepared:
            record = support.record(board, target)
            transfer.prepare(board, target, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
    assert not board.execute('SELECT 1 FROM containment_canonical_receipts WHERE task_id=?',(target,)).fetchone()
    rows = [custody.event(stop, hub, 1), change(board, peer, 4, 2, title='progress')]
    monkeypatch.setattr(client.acw, 'changes_page', lambda *a: dict(changes=[asdict(ch) for ch in rows], next=2, more=False))
    monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
    client.pull(board, hub, 'fixture', observers=False)
    assert kb.get_task(board, peer).title == 'progress'

@pytest.mark.parametrize('rows', [[], [{}]])
def test_scoped_malformed_finalization_preserves_sql_fence(board, rows):
    target, record = support.prepared(board)
    malformed = {**record, 'rows': rows}
    import sqlite3
    try:
        with dbc.write_txn(board):
            board.execute('INSERT INTO kanban_home_finalized1(task_id,nonce,receipt) VALUES (?,?,?)',
                          (target, record['nonce'], json.dumps(malformed)))
    except sqlite3.Error:
        pass
    assert transfer.frozen(board, target)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET title='invalid custody release' WHERE id=?",(target,))


@pytest.mark.parametrize('late', [False, True])
@pytest.mark.parametrize('upgrade', [False, True])
def test_certification_caches_only_proof_then_replays_after_exact_abort(board, monkeypatch, late, upgrade):
    from hermes_cli import kanban_home_feed as feed
    target, _, stop, _, hub = custody.scenario(board)
    sync.set_identity(board, 'fixture', 'worker-a3')
    replica.install(board)
    peer = kb.create_task(board, title='healthy')
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (target,))
        board.execute('DELETE FROM kanban_sync_outbox')
    record = support.record(board, target)
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone())
    events = [tuple(r) for r in board.execute('SELECT * FROM task_events WHERE task_id=?', (target,))]

    def prepare():
        with dbc.write_txn(board):
            transfer.prepare(board, target, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
            if upgrade:
                # Reconstruct the previous installed cache-table trigger, then
                # exercise the real dormant installer upgrade in this transaction.
                board.execute("CREATE TRIGGER home_prepared1_containment_canonical_receipts_insert "
                              "BEFORE INSERT ON containment_canonical_receipts "
                              "BEGIN SELECT RAISE(ABORT,'home transfer prepared'); END")
                transfer._install(board)

    original = hub.row_evidence
    def fetch(*args):
        proof = original(*args)
        if late and not transfer.frozen(board, target):
            prepare()
        return proof
    monkeypatch.setattr(hub, 'row_evidence', fetch)
    if not late:
        prepare()
    rows = [custody.event(stop, hub, 1), change(board, peer, 4, 2, title='progress')]
    page = dict(changes=[asdict(ch) for ch in rows], next=2, more=False)
    monkeypatch.setattr(client.acw, 'changes_page', lambda *a: page)
    monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
    client.pull(board, hub, 'fixture', observers=False)
    assert kb.get_task(board, peer).title == 'progress'
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone()) == before
    assert [tuple(r) for r in board.execute('SELECT * FROM task_events WHERE task_id=?', (target,))] == events
    assert sync.sync_state(board)['cursor'] == 2 and feed.pending(board)
    cached = tuple(board.execute('SELECT * FROM containment_canonical_receipts WHERE task_id=?', (target,)).fetchone())
    assert cached[-1] == json.dumps(hub.events[stop['event_uid']], sort_keys=True, separators=(',', ':'))
    # Transport proof may be appended only by the authenticated cache seam.
    with pytest.raises(sqlite3.IntegrityError, match='containment_pending'):
        with dbc.write_txn(board):
            board.execute('INSERT INTO containment_canonical_receipts VALUES (?,?,?,?,?,?,?)', ('uncertified', *cached[1:]))
    for query in ('DELETE FROM containment_canonical_receipts WHERE uid=?',
                  "UPDATE containment_canonical_receipts SET payload='{}' WHERE uid=?"):
        with pytest.raises(sqlite3.IntegrityError, match='containment_pending'):
            with dbc.write_txn(board):
                board.execute(query, (cached[0],))
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET title='no task authority' WHERE id=?", (target,))
    # Exact authoritative noncommit releases only the home preparation. The
    # retained protected event still enters ordinary containment verification.
    with dbc.write_txn(board):
        transfer.abort_source(board, {**record, 'phase': 'aborted', 'rows': []})
    page.update(changes=[])
    client.pull(board, hub, 'fixture', observers=False)
    assert not feed.pending(board)
    assert board.execute('SELECT kind FROM task_events WHERE uid=?', (stop['event_uid'],)).fetchone()[0] == 'contained'
    from hermes_cli.kanban_containment_connections import held
    assert held(board, target)


def malformed_manifest(record, fault):
    value = copy.deepcopy(record)
    row = value['rows'][0]
    rows = {
        'empty': [], 'empty-object': [{}], 'scalar': ['not-json'],
        'duplicate': [row, copy.deepcopy(row)],
        'unknown-table': [row, dict(table='unknown', uid='other', fields=dict(task_id=record['task']))],
        'unrelated': [row, dict(table='task_comments', uid='other', fields=dict(task_id='other'))],
        'missing-fields': [dict(table=row['table'], uid=row['uid'])],
    }
    if fault in rows:
        value['rows'] = rows[fault]
    else:
        row['fields'][fault] = {'version': True, 'created_on': 'worker-a2', 'deleted_at': 1}[fault]
    return value


@pytest.mark.parametrize('fault', ['empty', 'empty-object', 'scalar', 'duplicate', 'unknown-table',
                                   'unrelated', 'missing-fields', 'version', 'created_on', 'deleted_at'])
@pytest.mark.parametrize('historical', [False, True])
def test_manifest_guard_and_persisted_malformed_upgrade_stay_closed(board, fault, historical):
    from hermes_cli import kanban_home_feed as feed
    target, record = support.prepared(board)
    raw = json.dumps(malformed_manifest(record, fault))
    with dbc.write_txn(board):
        if historical:
            # Simulate the old install accepting a fully scoped malformed row;
            # leave it intact across upgrade: invalid history is not authority.
            board.execute('DROP TRIGGER home_finalized1_manifest')
            board.execute('INSERT INTO kanban_home_finalized1 VALUES (?,?,?)', (target, record['nonce'], raw))
            transfer._install(board)
        else:
            with pytest.raises(sqlite3.IntegrityError, match='home finalization manifest invalid'):
                board.execute('INSERT INTO kanban_home_finalized1 VALUES (?,?,?)', (target, record['nonce'], raw))
    assert transfer.frozen(board, target) and feed.active(board)
    ch = change(board, target, 8, 1, title='no authority')
    assert feed.blocked(board, ch)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET title='invalid release' WHERE id=?", (target,))
    assert board.execute('SELECT created_on,sync_version FROM tasks WHERE id=?', (target,)).fetchone()[:] == ('worker-a3', 7)


def test_legitimate_atomic_stop_and_ordinary_peer_still_work(board):
    target, record = support.prepared(board)
    peer = kb.create_task(board, title='ordinary')
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
        transfer.finalize_source(board, record)
        board.execute("UPDATE tasks SET title='peer progress' WHERE id=?", (peer,))
    assert kb.get_task(board, peer).title == 'peer progress'
    assert board.execute('SELECT created_on,sync_version FROM tasks WHERE id=?', (target,)).fetchone()[:] == ('hub', 8)

