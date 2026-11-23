"""Actual recovery/synthetic writers must not invent first-capture provenance. [daveey]

Certification remains dormant. New task INSERTs get immutable candidates only;
reconstructed predecessors stay UNKNOWN. This table drives all ten recovery
INSERT sites (including both divergence branches), three synthetic sites and
both identity rotations. It is behavior proof, not the declaration inventory.
"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_capture_client_cases as recovery
from tests.hermes_cli import kanban_capture_synthetic_cases as synthetic
from tests.hermes_cli import kanban_sync_support as support

OUTBOX = 'kanban_sync_outbox'
COLUMNS = ('capture_protocol', 'original_op_id', 'original_created_at', 'identity_state', 'exposure_history')


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    try:
        yield conn
    finally:
        conn.close()


def _outbox(board):
    return [dict(r) for r in board.execute('SELECT rowid,* FROM kanban_sync_outbox ORDER BY rowid')]


def _queue_rows(board, monkeypatch):
    # apply_sync_schema_steps repairs attached capture gaps through _queue_rows.
    client.ensure_identity(board, 'default', 'macbook')
    tid = kb.create_task(board, title='capture gap', assignee='worker')
    sync.drop_triggers(board)
    row_id = board.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'a','gap',1)",
                           (tid,)).lastrowid
    board.execute('DELETE FROM kanban_sync_outbox')
    path = board.execute('PRAGMA database_list').fetchone()[2]
    sync.install_or_remove(board, path)
    rows = _outbox(board)
    uid = board.execute('SELECT uid FROM task_comments WHERE id=?', (row_id,)).fetchone()[0]
    assert len(rows) == 1 and rows[0]['table_name'] == 'task_comments' and rows[0]['row_uid'] == uid
    return [(OUTBOX, rows[0]['rowid'])]


def _compat(board, monkeypatch, *, virtual=False):
    from hermes_cli import kanban_sync_compat as compat
    tid = kb.create_task(board, title='retained predecessor', assignee='worker', body='kept local')
    row = _outbox(board)[0]
    wire = {'fields': json.loads(row['fields']), 'hlc': '1-0-macbook'}
    field = 'workspace_kind' if virtual else 'body'
    assert field in wire['fields']
    answer = {'status': 'rejected', 'reason': 'forbidden_field', 'detail': f'not writable on tasks: {field}'}
    with kbc.write_txn(board):
        assert compat.enqueue_retry(board, row, wire, answer)
    rows = [r for r in _outbox(board) if r['op_id'] != row['op_id'] and r['table_name'] == 'tasks']
    assert len(rows) == 1 and rows[0]['row_uid'] == tid
    assert field not in json.loads(rows[0]['fields'])
    return [(OUTBOX, rows[0]['rowid'])]


def _drop_virtual(board, monkeypatch):
    return _compat(board, monkeypatch, virtual=True)


def _restore(board, monkeypatch):
    from hermes_cli import kanban_sync_compat as compat
    from hermes_cli import kanban_sync_omissions as omissions
    tid = kb.create_task(board, title='restore predecessor', assignee='worker', body='local held value')
    original = _outbox(board)[0]
    wire = {'fields': json.loads(original['fields']), 'hlc': '1-0-macbook'}
    with kbc.write_txn(board):
        assert compat.enqueue_retry(board, original, wire,
                                    {'reason': 'forbidden_field', 'detail': 'not writable on tasks: body'})
    before = {r['op_id'] for r in _outbox(board)}
    omissions.reconcile_page(board, 'tasks', [{'uid': tid, 'body': 'remote capability hint'}])
    rows = [r for r in _outbox(board) if r['op_id'] not in before]
    assert len(rows) == 1 and rows[0]['op'] == 'restore'
    assert json.loads(rows[0]['fields']) == {'body': 'local held value'}
    return [(OUTBOX, rows[0]['rowid'])]


def _synthetic_case(board, monkeypatch, name):
    pairs = synthetic.CASES[name](board, monkeypatch)
    return [(table, board.execute(f'SELECT rowid FROM {table} WHERE {synthetic.KEY[table]}=?',
                                 (key,)).fetchone()[0]) for table, key in pairs]


def _rotate(board, monkeypatch, *, transient=False, prior='original'):
    from hermes_cli.kanban_containment_outbox import strip_lifecycle
    client.ensure_identity(board, 'default', 'macbook')
    tid = kb.create_task(board, title='rotation', assignee='worker')
    board.execute('DELETE FROM kanban_sync_outbox')
    # Synthetic future certified and legacy fixtures exercise retention, not activation.
    history = json.dumps({'protocol': 'exposure1', 'coverage': 'unknown', 'certified': False,
                          'exposures': [{'principal': 'opaque-old-epoch'}]})
    protocol, original, created, state = ('capture1', 'old-operation', 123, 'original')
    if prior == 'candidate':
        protocol, state = 'capture_candidate1', None
    elif prior == 'unknown':
        protocol, original, created, state = None, None, None, None
    fields = json.dumps({'title': 'keep', 'status': 'ready'})
    board.execute('INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,'
                  'capture_protocol,original_op_id,original_created_at,identity_state,exposure_history) '
                  "VALUES ('old-operation','tasks',?,'upsert',?,'',123,?,?,?,?,?)",
                  (tid, fields, protocol, original, created, state, history))
    if transient:
        def refuse(_board, ops):
            return {'results': [{'client_op_id': o['client_op_id'], 'status': 'rejected',
                                 'reason': 'storage_error'} for o in ops]}
        result = client.push(board, SimpleNamespace(ops=refuse), 'default', 'macbook',
                             primary=False, max_batches=1)
        assert result.deferred == 1 and result.rejected == 0
    else:
        with kbc.write_txn(board):
            strip_lifecycle(board, tid)
    rows = _outbox(board)
    assert len(rows) == 1 and rows[0]['op_id'] != 'old-operation'
    assert tuple(rows[0][c] for c in COLUMNS) == (protocol, original, created, 'ambiguous', history)
    if not transient:
        assert json.loads(rows[0]['fields']) == {'title': 'keep'}
    return [(OUTBOX, rows[0]['rowid'])]


CASES = {
    'queue_rows': _queue_rows,
    'import_board': recovery.case_import_board,
    'repair_missing_children': recovery.case_repair_missing_children,
    'retire_lost_placeholder_links': recovery.case_retire_lost_placeholder_links,
    'enqueue_retry': _compat,
    'drop_virtual': _drop_virtual,
    'repair_divergence_tasks': recovery.case_repair_divergence_task,
    'repair_divergence_runs': recovery.case_repair_divergence_run,
    'queue_restore': _restore,
    **{name: None for name in synthetic.CASES},
    'strip_lifecycle': None,
    'legacy_transient': None,
}


@pytest.mark.parametrize('site', CASES)
def test_uncertain_predecessor_never_becomes_a_new_capture(board, monkeypatch, site):
    if site in synthetic.CASES:
        rows = _synthetic_case(board, monkeypatch, site)
    elif site in ('strip_lifecycle', 'legacy_transient'):
        rows = _rotate(board, monkeypatch, transient=site == 'legacy_transient')
    else:
        rows = CASES[site](board, monkeypatch)
    assert rows, site
    if site not in ('strip_lifecycle', 'legacy_transient'):
        for table, rowid in rows:
            row = board.execute(f"SELECT {','.join(COLUMNS)} FROM {table} WHERE rowid=?", (rowid,)).fetchone()
            assert row is not None and tuple(row) == (None,) * len(COLUMNS), (site, tuple(row))
    # Genuine-new task INSERT remains the contrasting dormant candidate path.
    tid = kb.create_task(board, title='genuinely new intent', assignee='worker')
    row = board.execute('SELECT * FROM kanban_sync_outbox WHERE row_uid=? AND table_name=\'tasks\'', (tid,)).fetchone()
    assert tuple(row[c] for c in COLUMNS[:4]) == ('capture_candidate1', row['op_id'], row['created_at'], None)
    assert json.loads(row['exposure_history']) == {
        'protocol': 'exposure1', 'coverage': 'candidate', 'certified': False, 'exposures': []}


@pytest.mark.parametrize('transient', [False, True], ids=['strip_lifecycle', 'legacy_transient'])
@pytest.mark.parametrize('prior', ['candidate', 'unknown'])
def test_rotation_preserves_dormant_or_missing_history(board, monkeypatch, transient, prior):
    _rotate(board, monkeypatch, transient=transient, prior=prior)
