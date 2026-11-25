"""Native quarantine restrictive appends compose with prepared home custody."""
from dataclasses import asdict, replace
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_transfer as transfer, kanban_sync_client as client
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import test_kanban_home_source_stop as support
from tests.hermes_cli import test_kanban_containment_replica as custody
from tests.hermes_cli.test_review_home_feed_isolation import change
board = support.board


def prepare(board, task):
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (task,))
        board.execute('DELETE FROM kanban_sync_outbox')
        record = {**support.record(board, task), 'nonce': f'quarantine-attempt-{task}'}
        transfer.prepare(board, task, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
    return record


@pytest.mark.parametrize('boundary', ['before', 'fetch', 'writer', 'upgrade'])
@pytest.mark.parametrize('existing', [False, True])
def test_quarantine_closure_races_preserve_custody(board, monkeypatch, boundary, existing):
    target, _, stop, _, hub = custody.scenario(board)
    sync.set_identity(board, 'fixture', 'worker-a3')
    with dbc.write_txn(board):
        quarantine.install(board)
    child = kb.create_task(board, title='child')
    grandchild = kb.create_task(board, title='grandchild')
    peer = kb.create_task(board, title='healthy')
    protected = replace(custody.event(stop, hub, 1), origin_gateway='wrong-origin')
    if existing:
        quarantine.certified_page(board, hub, 'fixture', [protected])
    records = []
    prepared_rows = {}

    def prepare_all():
        for task in (target, child, grandchild):
            records.append(prepare(board, task))
            prepared_rows[task] = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())

    if boundary in ('before', 'upgrade'):
        prepare_all()
    if boundary == 'upgrade':
        with dbc.write_txn(board):
            for table in ('containment_sync_quarantine', 'containment_sync_quarantine_tasks'):
                board.execute(f'DROP TRIGGER home_prepared1_{table}_insert')
                board.execute(f"CREATE TRIGGER home_prepared1_{table}_insert BEFORE INSERT ON {table} "
                              "BEGIN SELECT RAISE(ABORT,'home transfer prepared'); END")
            transfer._install(board)
    original_fetch = hub.row_evidence
    original_withhold = quarantine.withhold

    def fetch(*args):
        result = original_fetch(*args)
        if boundary == 'fetch' and not records:
            prepare_all()
        return result

    def withhold(conn, changes):
        if boundary == 'writer' and not records:
            prepare_all()
        return original_withhold(conn, changes)

    monkeypatch.setattr(hub, 'row_evidence', fetch)
    monkeypatch.setattr(quarantine, 'withhold', withhold)
    # Existing-quarantine pages may contain no protected row to fetch; certify
    # the same conflict again only for that fetch-race case.
    rows = [protected] if not existing or boundary == 'fetch' else []
    for seq, (parent, descendant) in enumerate(((child, grandchild), (target, child)), 2):
        rows.append(sync.RemoteChange(table='task_links', uid=f'{parent}>{descendant}', op='upsert',
                                      seq=seq, origin_gateway='hub', version=seq, hlc='2000-0-hub',
                                      fields={'parent_id': parent, 'child_id': descendant}))
    rows.append(change(board, peer, 9, 4, title='progress'))
    before = {task: tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
              for task in (target, child, grandchild)}
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    events = list(map(tuple, board.execute('SELECT * FROM task_events')))
    monkeypatch.setattr(client.acw, 'changes_page', lambda *a: dict(changes=[asdict(ch) for ch in rows], next=9, more=False))
    monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
    client.pull(board, hub, 'fixture', observers=False)
    assert kb.get_task(board, peer).title == 'progress'
    assert sync.sync_state(board)['cursor'] == 9
    assert {target, child, grandchild} <= quarantine.tasks(board)
    assert board.execute('SELECT COUNT(*) FROM containment_sync_withheld').fetchone()[0] >= 2
    # Preparation itself changes sync_version only in these fixture races.
    for task, original in before.items():
        current = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        assert current == prepared_rows[task]
        if boundary in ('before', 'upgrade'):
            assert current == original
        assert kb.get_task(board, task).title != 'progress'
    assert list(map(tuple, board.execute('SELECT * FROM task_events'))) == events
    import sqlite3
    for table in ('containment_sync_quarantine', 'containment_sync_quarantine_tasks'):
        prior = list(map(tuple, board.execute(f'SELECT * FROM {table}')))
        for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET task_id=task_id'):
            with pytest.raises(sqlite3.IntegrityError):
                with dbc.write_txn(board):
                    board.execute(statement)
            assert list(map(tuple, board.execute(f'SELECT * FROM {table}'))) == prior
        columns = [row[1] for row in board.execute(f'PRAGMA table_info({table})')]
        row = list(prior[0])
        row[columns.index('reason' if table == 'containment_sync_quarantine' else 'via')] = 'changed'
        with dbc.write_txn(board):
            board.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in row)})", row)
        assert list(map(tuple, board.execute(f'SELECT * FROM {table}'))) == prior
    for record in records:
        with dbc.write_txn(board):
            transfer.abort_source(board, {**record, 'phase': 'aborted', 'rows': []})
    # Abort is not quarantine-release authority; repeat preserves all holds.
    client.pull(board, hub, 'fixture', observers=False)
    assert {target, child, grandchild} <= quarantine.tasks(board)

@pytest.mark.parametrize('phase', ['ordinary', 'prepared', 'aborted'])
@pytest.mark.parametrize('existing', [False, True])
def test_conflicting_protected_row_preserves_healthy_peer(board, monkeypatch, phase, existing):
    target, _, stop, _, hub = custody.scenario(board)
    sync.set_identity(board, 'fixture', 'worker-a3')
    with dbc.write_txn(board):
        quarantine.install(board)
    peer = kb.create_task(board, title='healthy')
    protected = replace(custody.event(stop, hub, 1), origin_gateway='wrong-origin')
    if existing:
        assert quarantine.certified_page(board, hub, 'fixture', [protected]) == []
        assert quarantine.held(board, target)
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (target,))
        board.execute('DELETE FROM kanban_sync_outbox')
        if phase != 'ordinary':
            record = support.record(board, target)
            transfer.prepare(board, target, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
            if phase == 'aborted':
                transfer.abort_source(board, {**record, 'phase': 'aborted', 'rows': []})
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone())
    rows = ([] if existing else [protected]) + [change(board, peer, 4, 2, title='progress')]
    monkeypatch.setattr(client.acw, 'changes_page', lambda *a: dict(changes=[asdict(ch) for ch in rows], next=2, more=False))
    monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
    client.pull(board, hub, 'fixture', observers=False)
    assert kb.get_task(board, peer).title == 'progress'
    assert quarantine.held(board, target)
    assert sync.sync_state(board)['cursor'] == 2
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone()) == before
