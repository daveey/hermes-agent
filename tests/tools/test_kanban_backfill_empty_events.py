"""Incremental empty-creator classification retains Python semantics and retries."""
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_creator_chats as creator
from hermes_cli import kanban_db_connect as kbc
from tests.tools.test_kanban_worker_created_chats import make_home


# Literal pre-optimization SELECT; only e.id was added for marker identity.
ORIGINAL_ROWS = ("SELECT t.id, t.session_id, e.payload, e.id AS creator_event_id "
                 "FROM tasks t JOIN task_events e ON e.task_id=t.id AND e.kind='created' "
                 "WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
                 "AND done.kind='creator_chats_resolved')")
# Independent copies (not imported from production) of the approved E1 shape and the pre-patch SELECT.
ENVELOPE = (f"SELECT * FROM ({ORIGINAL_ROWS} LIMIT -1 OFFSET 0) AS c WHERE NOT EXISTS "
            "(SELECT 1 FROM kanban_creator_empty_events m WHERE m.event_id=c.creator_event_id)")
PRE_PATCH = ("SELECT t.id, t.session_id, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
             "AND e.kind='created' WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
             "AND done.kind='creator_chats_resolved')")


@pytest.fixture
def board(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch)
    with kbc.connect_closing() as conn:
        yield conn


def seed(conn, payload, event_id=123):
    conn.execute("INSERT OR IGNORE INTO tasks (id,title,assignee,status,created_at) "
                 "VALUES ('empty','empty','default','todo',1)")
    conn.execute("INSERT OR REPLACE INTO task_events (id,task_id,kind,payload,created_at) "
                 "VALUES (?,'empty','created',?,1)", (event_id, payload))
    conn.commit()


def decoded(monkeypatch, payload):
    original = json.loads
    calls = []

    def track(value, *args, **kwargs):
        if value == (payload or '{}'):
            calls.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', track)
    return calls


@pytest.mark.parametrize('payload', [None, '', '{}', b'{}',
    *[json.dumps({'creator_task_id': value}) for value in [None, False, 0, '', [], {}]],
    '{"creator_task_id":"missing","creator_task_id":null}'])
def test_empty_event_decoded_once_across_passes(board, monkeypatch, payload):
    seed(board, payload)
    calls = decoded(monkeypatch, payload)
    assert creator.backfill(board) == {'attempted': [], 'unresolved': []}
    assert calls
    calls.clear()
    assert creator.backfill(board) == {'attempted': [], 'unresolved': []}
    assert calls == []


@pytest.mark.parametrize('payload,exception', [('broken', ValueError), ('[]', AttributeError),
                                               ('null', AttributeError), ('0', AttributeError)])
def test_invalid_payload_never_certified(board, payload, exception):
    seed(board, payload)
    for _ in range(2):
        with pytest.raises(exception):
            creator.backfill(board)


@pytest.mark.parametrize('mutation', ['update', 'replace'])
def test_changed_empty_event_retried(board, monkeypatch, mutation):
    seed(board, '{}')
    creator.backfill(board)
    payload = '{"creator_task_id":"missing"}'
    if mutation == 'update':
        board.execute('UPDATE task_events SET payload=? WHERE id=123', (payload,))
        board.commit()
    else:
        board.execute('PRAGMA recursive_triggers=OFF')
        seed(board, payload)
    calls = []
    monkeypatch.setattr(creator, 'inherit_chats', lambda conn, tid, parent, **kw: calls.append((tid, parent)))
    for _ in range(2):
        assert creator.backfill(board) == {'attempted': ['empty'], 'unresolved': ['empty']}
    assert calls == [('empty', 'missing'), ('empty', 'missing')]


def test_new_event_below_old_id_is_discovered(board, monkeypatch):
    seed(board, '{}', 200)
    creator.backfill(board)
    seed(board, '{"creator_task_id":"new"}', 100)
    calls = []
    monkeypatch.setattr(creator, 'inherit_chats', lambda conn, tid, parent, **kw: calls.append(parent))
    creator.backfill(board)
    assert calls == ['new']


def test_marker_install_second_pass_has_no_schema_change(board):
    seed(board, '{}')
    creator.backfill(board)
    cookie = board.execute('PRAGMA schema_version').fetchone()[0]
    changes = board.total_changes
    creator.backfill(board)
    assert board.execute('PRAGMA schema_version').fetchone()[0] == cookie
    assert board.total_changes == changes


def test_only_marked_rows_need_no_verdict_read(board, monkeypatch):
    seed(board, '{}')
    creator.backfill(board)
    monkeypatch.setattr(creator.LinkVerdicts, 'read', lambda *a: pytest.fail('marked rows read verdicts'))
    assert creator.backfill(board) == {'attempted': [], 'unresolved': []}


def test_missing_trigger_clears_old_markers(board, monkeypatch):
    seed(board, '{}')
    creator.backfill(board)
    names = [r[0] for r in board.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                        "AND name GLOB 'creator_empty_*'")]
    assert names, 'no incremental invalidation protocol installed'
    for name in names:
        board.execute(f'DROP TRIGGER {name}')
    board.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"late"}',))
    board.commit()
    calls = []
    monkeypatch.setattr(creator, 'inherit_chats', lambda conn, tid, parent, **kw: calls.append(parent))
    creator.backfill(board)
    assert calls == ['late']


def test_failed_pass_rolls_back_markers(board):
    seed(board, '{}', 123)
    seed(board, 'broken', 124)
    with pytest.raises(ValueError):
        creator.backfill(board)
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 0


def test_trigger_gap_after_ensure_cannot_hide_changed_row(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    original = empty.ensure

    def gap(conn):
        assert original(conn)
        conn.execute('DROP TRIGGER creator_empty_update')
        conn.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"gap"}',))
        conn.commit()
        return True

    monkeypatch.setattr(empty, 'ensure', gap)
    assert [r['id'] for r in empty.candidates(board)] == ['empty']


def test_stale_selected_payload_is_not_marked(board, monkeypatch):
    from hermes_cli import kanban_db_notify as notify
    seed(board, '{}')
    original = notify.headless_sessions

    def rewrite(sessions):
        path = board.execute('PRAGMA database_list').fetchone()[2]
        peer = sqlite3.connect(path, isolation_level=None)
        try:
            peer.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"raced"}',))
        finally:
            peer.close()
        return original(sessions)

    monkeypatch.setattr(notify, 'headless_sessions', rewrite)
    creator.backfill(board)
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 0
    calls = []
    monkeypatch.setattr(creator, 'inherit_chats', lambda conn, tid, parent, **kw: calls.append(parent))
    creator.backfill(board)
    assert calls == ['raced']


def test_install_failure_uses_original_candidates(board):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    board.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                         if action == sqlite3.SQLITE_CREATE_TABLE else sqlite3.SQLITE_OK)
    try:
        assert [r['id'] for r in empty.candidates(board)] == ['empty']
    finally:
        board.set_authorizer(None)


def test_marker_insert_failure_does_not_abort_backfill(board):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    assert empty.ensure(board)
    board.set_authorizer(lambda action, name, *args: sqlite3.SQLITE_DENY
                         if action == sqlite3.SQLITE_INSERT and name == empty._TABLE else sqlite3.SQLITE_OK)
    try:
        assert creator.backfill(board) == {'attempted': [], 'unresolved': []}
    finally:
        board.set_authorizer(None)
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 0


def test_unknown_table_layout_is_not_replaced(board):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    board.execute('CREATE TABLE kanban_creator_empty_events (future TEXT)')
    board.commit()
    before = board.execute('PRAGMA schema_version').fetchone()[0]
    assert [r['id'] for r in empty.candidates(board)] == ['empty']
    assert board.execute('PRAGMA schema_version').fetchone()[0] == before


@pytest.mark.parametrize('analyze', [False, True])
def test_candidate_order_retains_literal_driver(board, analyze):
    from hermes_cli import kanban_creator_empty as empty
    for event_id in [40, 20, 30]:
        seed(board, '{}', event_id)
    if analyze:
        board.execute('ANALYZE')
    expected = [tuple(r) for r in board.execute(ORIGINAL_ROWS)]
    assert [tuple(r) for r in empty.candidates(board)] == expected


def test_other_installers_preserve_marker_protocol(board):
    from hermes_cli import kanban_creator_empty as empty
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_repair_revision as repair
    from hermes_cli import kanban_activity_revision as activity
    seed(board, '{}')
    creator.backfill(board)
    sync.install_triggers(board)
    repair.ensure_revision(board)
    activity.ensure_revision(board, ('tasks', 'task_events'))
    assert empty._schema(board) == empty._EXPECTED
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 1


def test_human_session_still_skipped_before_invalid_decode(board, monkeypatch):
    from hermes_cli import kanban_db_notify as notify
    seed(board, 'broken')
    board.execute("UPDATE tasks SET session_id='human' WHERE id='empty'")
    board.commit()
    monkeypatch.setattr(notify, 'headless_sessions', lambda sessions: set())
    assert creator.backfill(board) == {'attempted': [], 'unresolved': []}
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 0


def test_truthy_creator_keeps_verdict_read_after_empty_classification(board, monkeypatch):
    seed(board, '{}')
    creator.backfill(board)
    seed(board, '{"creator_task_id":"missing"}', 124)
    calls = []
    monkeypatch.setattr(creator.LinkVerdicts, 'read', lambda *a: calls.append(True))
    monkeypatch.setattr(creator, 'inherit_chats', lambda *a, **kw: None)
    assert creator.backfill(board)['unresolved'] == ['empty']
    assert calls == [True]


@pytest.mark.parametrize('analyze', [False, True])
def test_shuffled_multitask_order_and_same_pass_descendants(board, monkeypatch, analyze):
    import random
    from hermes_cli import kanban_creator_empty as empty
    ids = [f'task-{i:04}' for i in range(2000)]
    random.Random(71).shuffle(ids)
    board.execute('BEGIN')
    for i, task_id in enumerate(ids):
        board.execute("INSERT INTO tasks (id,title,assignee,status,created_at) VALUES (?,?,'default','todo',1)",
                      (task_id, task_id))
        board.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,'created',?,1)",
                      (task_id, json.dumps({'creator_task_id': 'root'})))
        board.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,'comment','{}',1)",
                      (task_id,))
    board.commit()
    assert empty.ensure(board)
    if analyze:
        board.execute('ANALYZE')
    literal = list(board.execute(ORIGINAL_ROWS))
    pre_patch_sql = (
        "SELECT t.id, t.session_id, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
        "AND e.kind='created' WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
        "AND done.kind='creator_chats_resolved')")
    assert [tuple(r)[:3] for r in literal] == [tuple(r) for r in board.execute(pre_patch_sql)]
    assert [tuple(r) for r in board.execute('EXPLAIN QUERY PLAN ' + ORIGINAL_ROWS)] == [
        tuple(r) for r in board.execute('EXPLAIN QUERY PLAN ' + pre_patch_sql)]
    if analyze:
        assert [r['id'] for r in literal] != sorted(ids)
    # Certify alternating rows; the survivors must remain a stable subsequence.
    marked = {r['creator_event_id'] for r in literal[::2]}
    board.executemany('INSERT INTO kanban_creator_empty_events VALUES (?)', [(i,) for i in marked])
    board.commit()
    wanted = [tuple(r) for r in literal if r['creator_event_id'] not in marked]
    assert [tuple(r) for r in empty.candidates(board)] == wanted
    observed = []

    def inherit(conn, task_id, parent, **kwargs):
        # Model same-pass ancestor visibility: each row depends on its literal predecessor.
        position = [r[0] for r in wanted].index(task_id)
        assert position == len(observed)
        if observed:
            assert conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                                (observed[-1],)).fetchone()
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                     "VALUES (?,'creator_chats_resolved','{}',1)", (task_id,))
        observed.append(task_id)

    monkeypatch.setattr(creator, 'inherit_chats', inherit)
    assert creator.backfill(board)['attempted'] == [r[0] for r in wanted]
    assert observed == [r[0] for r in wanted]


def test_candidates_in_existing_transaction_fall_back_unfiltered(board):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    board.execute('BEGIN')
    try:
        assert [tuple(r) for r in empty.candidates(board)] == [tuple(r) for r in board.execute(ORIGINAL_ROWS)]
        assert board.in_transaction
    finally:
        board.rollback()


class MarkerFailure:
    """Inject at SQLite's insert boundary, retaining the real outer transaction."""
    def __init__(self, conn, *, rollback, code):
        self.conn, self.rollback_on_insert, self.code = conn, rollback, code

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, parameters=()):
        if sql.startswith('INSERT OR IGNORE INTO kanban_creator_empty_events'):
            if self.rollback_on_insert:
                self.conn.rollback()
            error = sqlite3.DatabaseError('injected marker failure')
            error.sqlite_errorcode = self.code
            raise error
        return self.conn.execute(sql, parameters)


@pytest.mark.parametrize('rollback,code', [(True, sqlite3.SQLITE_FULL),
                                         (False, sqlite3.SQLITE_CORRUPT),
                                         (False, sqlite3.SQLITE_NOTADB),
                                         (False, sqlite3.SQLITE_CORRUPT | (1 << 8))])
def test_marker_failure_never_allows_later_autocommit(board, monkeypatch, rollback, code):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}', 123)
    seed(board, '{"creator_task_id":"late"}', 124)
    assert empty.ensure(board)
    later = []

    def inherit(conn, *args, **kwargs):
        later.append(conn.in_transaction)
        conn.execute("UPDATE tasks SET title='escaped' WHERE id='empty'")

    monkeypatch.setattr(creator, 'inherit_chats', inherit)
    with pytest.raises(sqlite3.DatabaseError, match='injected marker failure'):
        creator.backfill(MarkerFailure(board, rollback=rollback, code=code))
    assert later == []
    assert board.execute("SELECT title FROM tasks WHERE id='empty'").fetchone()[0] == 'empty'
    assert not board.in_transaction


@pytest.mark.parametrize('ddl', [
    'CREATE TABLE kanban_creator_empty_events (future TEXT)',
    'CREATE VIEW kanban_creator_empty_events AS SELECT 1 AS event_id',
    'CREATE TABLE creator_empty_update (future TEXT)',
    'CREATE VIEW creator_empty_insert AS SELECT 1',
    'CREATE INDEX creator_empty_delete ON task_events(kind)',
])
def test_unknown_schema_rejected_without_writer_or_commit(board, ddl):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    board.execute(ddl)
    before = board.execute('PRAGMA schema_version').fetchone()[0]
    statements = []
    board.set_trace_callback(statements.append)
    try:
        assert not empty.ensure(board)
        assert [r['id'] for r in empty.candidates(board)] == ['empty']
    finally:
        board.set_trace_callback(None)
    assert board.execute('PRAGMA schema_version').fetchone()[0] == before
    assert not any(s.upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK', 'DROP', 'DELETE', 'CREATE'))
                   for s in statements), statements


def test_cold_pass_checks_marker_coverage_once_under_write_lock(board):
    from hermes_cli import kanban_creator_empty as empty
    for event_id in range(123, 143):
        seed(board, '{}', event_id)
    assert empty.ensure(board)
    checks = []
    active = False

    def trace(sql):
        nonlocal active
        if sql == 'BEGIN IMMEDIATE':
            active = True
        elif sql in ('COMMIT', 'ROLLBACK'):
            active = False
        if active and 'sqlite_master' in sql and 'creator_empty' in sql:
            checks.append(sql)

    board.set_trace_callback(trace)
    try:
        creator.backfill(board)
    finally:
        board.set_trace_callback(None)
    assert len(checks) == 1, checks
    assert board.execute('SELECT COUNT(*) FROM kanban_creator_empty_events').fetchone()[0] == 20


def test_same_pass_real_inheritance_reaches_beyond_walk_depth(board):
    from hermes_cli import kanban_chat_links as links
    board.execute('BEGIN')
    ids = ['root', *[f'node-{(i * 17) % 41:02}' for i in range(41)]]
    for tid in ids:
        board.execute("INSERT INTO tasks(id,title,assignee,status,created_at) VALUES (?,?,'default','todo',1)",
                      (tid, tid))
        if tid != 'root':
            board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'created','{}',1)",
                          (tid,))
    board.execute("INSERT INTO task_chat_links(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                  "VALUES (?,'root','away','default','human','created',0,1)",
                  (links.link_uid('root', 'away', 'default', 'human', 'created'),))
    board.commit()
    board.execute('ANALYZE')
    order = [r['id'] for r in board.execute(ORIGINAL_ROWS)]
    previous = 'root'
    for tid in order:
        board.execute("UPDATE task_events SET payload=? WHERE task_id=?",
                      (json.dumps({'creator_task_id': previous}), tid))
        previous = tid
    board.commit()
    assert len(order) > creator.MAX_DEPTH
    assert creator.backfill(board) == {'attempted': order, 'unresolved': []}
    assert board.execute("SELECT COUNT(*) FROM task_chat_links WHERE chat_session='human'").fetchone()[0] == 42


def test_candidate_reads_rows_schema_and_markers_in_one_deferred_snapshot(board):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    statements = []
    board.set_trace_callback(statements.append)
    try:
        assert empty.candidates(board) == []
    finally:
        board.set_trace_callback(None)
    start = statements.index('BEGIN')
    schema = next(i for i, sql in enumerate(statements) if i > start and 'sqlite_master' in sql)
    rows = statements.index(ENVELOPE)  # coverage precedes the one filtered statement
    assert start < schema < rows < statements.index('ROLLBACK')
    assert ORIGINAL_ROWS not in statements and not any('SELECT event_id FROM' in s for s in statements)
    assert not any(sql == 'BEGIN IMMEDIATE' for sql in statements)
    assert not board.in_transaction


def test_write_coverage_fails_closed_without_transaction_or_schema(board):
    from hermes_cli import kanban_creator_empty as empty
    assert not empty.write_coverage(board)
    board.execute('BEGIN IMMEDIATE')
    try:
        assert not empty.write_coverage(board)
    finally:
        board.rollback()


@pytest.mark.parametrize('code,rollback', [(sqlite3.SQLITE_AUTH, False),
                                         (sqlite3.SQLITE_CORRUPT, False),
                                         (sqlite3.SQLITE_NOTADB, False),
                                         (sqlite3.SQLITE_IOERR, True)])
def test_write_coverage_error_retains_only_harmless_fallback(board, monkeypatch, code, rollback):
    from hermes_cli import kanban_creator_empty as empty
    board.execute('BEGIN IMMEDIATE')

    def fail_schema(conn):
        if rollback:
            conn.rollback()
        exc = sqlite3.DatabaseError('coverage refused')
        exc.sqlite_errorcode = code
        raise exc

    monkeypatch.setattr(empty, '_schema', fail_schema)
    try:
        if code == sqlite3.SQLITE_AUTH:
            assert not empty.write_coverage(board)
            assert board.in_transaction
        else:
            with pytest.raises(sqlite3.DatabaseError, match='coverage refused'):
                empty.write_coverage(board)
    finally:
        board.rollback()


def test_ensure_rechecks_unknown_schema_after_acquiring_writer(board, monkeypatch):
    import contextlib
    from hermes_cli import kanban_creator_empty as empty
    original = kbc.write_txn

    @contextlib.contextmanager
    def race(conn):
        conn.execute('CREATE TABLE kanban_creator_empty_events(future TEXT)')
        with original(conn):
            yield

    monkeypatch.setattr(kbc, 'write_txn', race)
    assert not empty.ensure(board)
    assert board.execute('PRAGMA table_info(kanban_creator_empty_events)').fetchone()[1] == 'future'
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'creator_empty_%'").fetchone()


@pytest.mark.parametrize('gap', ['changed-trigger', 'missing-table'])
def test_reinstall_after_schema_gap_discards_all_certificates(board, gap):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    if gap == 'changed-trigger':
        board.execute('DROP TRIGGER creator_empty_update')
        board.execute('CREATE TRIGGER creator_empty_update AFTER UPDATE ON task_events BEGIN SELECT 1; END')
        board.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"late"}',))
    else:
        board.execute('DROP TABLE kanban_creator_empty_events')
    board.commit()
    assert empty.ensure(board)
    assert empty._schema(board) == empty._EXPECTED
    assert board.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []
    assert [r['id'] for r in empty.candidates(board)] == ['empty']


def test_marker_survives_reopened_connection(board, monkeypatch):
    seed(board, '{}')
    creator.backfill(board)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    calls = decoded(monkeypatch, '{}')
    with kbc.connect_closing(Path(path)) as reopened:
        assert creator.backfill(reopened) == {'attempted': [], 'unresolved': []}
    assert calls == []


@pytest.mark.parametrize('mismatch', ['event-id', 'task-id', 'kind'])
def test_remember_selected_identity_predicates_independently(board, mismatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    assert empty.ensure(board)
    row = board.execute(ORIGINAL_ROWS).fetchone()
    if mismatch == 'event-id':
        # An otherwise-identical second event must not be certified along with the selected event.
        seed(board, '{}', 124)
    elif mismatch == 'task-id':
        board.execute("INSERT INTO tasks(id,title,assignee,status,created_at) VALUES ('other','other','default','todo',1)")
        board.execute("UPDATE task_events SET task_id='other' WHERE id=123")
        board.commit()
    else:
        board.execute("UPDATE task_events SET kind='comment' WHERE id=123")
        board.commit()
    with kbc.write_txn(board):
        empty.remember_empty(board, row)
    assert [r[0] for r in board.execute('SELECT event_id FROM kanban_creator_empty_events')] == (
        [123] if mismatch == 'event-id' else [])


def test_ensure_peer_completed_install_rechecked_without_ddl(board, monkeypatch):
    import contextlib
    from hermes_cli import kanban_creator_empty as empty
    original = kbc.write_txn
    statements = []

    @contextlib.contextmanager
    def race(conn):
        # Model a peer installing between the first schema read and our writer lock.
        monkeypatch.setattr(kbc, 'write_txn', original)
        assert empty.ensure(conn)
        with original(conn):
            conn.set_trace_callback(statements.append)
            try:
                yield
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(kbc, 'write_txn', race)
    assert empty.ensure(board)
    assert not any(s.startswith(('CREATE', 'DROP', 'DELETE')) for s in statements), statements


def test_candidates_schema_read_refusal_returns_unfiltered_and_releases_snapshot(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    original = empty._schema

    def refuse_in_snapshot(conn):
        if conn.in_transaction:
            raise sqlite3.DatabaseError('read refused')
        return original(conn)

    monkeypatch.setattr(empty, '_schema', refuse_in_snapshot)
    assert [r['id'] for r in empty.candidates(board)] == ['empty']
    assert not board.in_transaction


def test_write_coverage_requires_writer_even_with_intact_schema(board):
    from hermes_cli import kanban_creator_empty as empty
    assert empty.ensure(board)
    assert not empty.write_coverage(board)
    with kbc.write_txn(board):
        assert empty.write_coverage(board)


@pytest.mark.parametrize('code', [sqlite3.SQLITE_CORRUPT | (1 << 8), sqlite3.SQLITE_NOTADB | (1 << 8)])
def test_write_coverage_extended_corruption_rethrown(board, monkeypatch, code):
    from hermes_cli import kanban_creator_empty as empty

    def refuse(conn):
        exc = sqlite3.DatabaseError('extended corruption')
        exc.sqlite_errorcode = code
        raise exc

    monkeypatch.setattr(empty, '_schema', refuse)
    with kbc.write_txn(board):
        with pytest.raises(sqlite3.DatabaseError, match='extended corruption'):
            empty.write_coverage(board)


def test_backfill_does_not_remember_without_writer_coverage(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    monkeypatch.setattr(empty, 'write_coverage', lambda conn: False)
    monkeypatch.setattr(empty, 'remember_empty', lambda *a: pytest.fail('uncovered certificate write'))
    assert creator.backfill(board) == {'attempted': [], 'unresolved': []}


@pytest.mark.parametrize('operation', ['insert', 'delete', 'update-old', 'update-new'])
def test_invalidation_independently_removes_affected_identity(board, operation):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    assert empty.ensure(board)
    # The cache is disposable: even orphan marker IDs must be invalidated on insertion/rekey.
    event_id = 124 if operation in ('insert', 'update-new') else 123
    board.execute('INSERT INTO kanban_creator_empty_events VALUES (?)', (event_id,))
    if operation == 'insert':
        board.execute("INSERT INTO task_events(id,task_id,kind,payload,created_at) VALUES (124,'empty','created','{}',1)")
    elif operation == 'delete':
        board.execute('DELETE FROM task_events WHERE id=123')
    else:
        board.execute('UPDATE task_events SET id=124 WHERE id=123')
    assert board.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []
    board.rollback()


def test_intact_ensure_never_acquires_writer(board):
    from hermes_cli import kanban_creator_empty as empty
    assert empty.ensure(board)
    statements = []
    board.set_trace_callback(statements.append)
    try:
        assert empty.ensure(board)
    finally:
        board.set_trace_callback(None)
    assert not any(s.startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for s in statements), statements


def test_insert_invalidation_without_unrelated_uid_rewrite_trigger():
    from hermes_cli import kanban_creator_empty as empty
    # Reduced valid table shape: insert invalidation must not depend on the full
    # board's UID-maintenance UPDATE trigger incidentally invalidating the ID.
    with sqlite3.connect(':memory:', isolation_level=None) as conn:
        conn.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY)')
        assert empty.ensure(conn)
        conn.execute('INSERT INTO kanban_creator_empty_events VALUES (7)')
        conn.execute('INSERT INTO task_events VALUES (7)')
        assert conn.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []


# ---------------------------------------------------------------------------------------------
# Order-preserving envelope (E1): literal rows in literal order, filtered inside one statement.
# ---------------------------------------------------------------------------------------------
def details(conn, sql):
    return [r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + sql)]


def populate(conn, n, extra, seed):
    """Shuffled task rowids, shuffled event ids, duplicates, resolved, human sessions, mixed payloads."""
    import random
    rnd = random.Random(seed)
    ids = [f't{rnd.randrange(10 ** 9):09}-{i}' for i in range(n)]
    events = []
    for tid in ids:
        kind = rnd.random()
        payload = None if kind < .3 else '{}' if kind < .6 else json.dumps({'creator_task_id': rnd.choice(ids)})
        events.append((tid, 'created', payload))
        if rnd.random() < .01:
            events.append((tid, 'created', '{}'))
        if rnd.random() < .10:
            events.append((tid, 'creator_chats_resolved', '{}'))
        events += [(tid, 'comment', '{}') for _ in range(extra)]
    rnd.shuffle(events)
    conn.execute('BEGIN')
    for tid in ids:
        conn.execute("INSERT INTO tasks (id,title,assignee,status,created_at,session_id) "
                     "VALUES (?,?,'default','todo',1,?)", (tid, tid, 'human' if rnd.random() < .2 else None))
    conn.executemany("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,1)", events)
    conn.commit()
    return ids


def mark_falsy(conn, seed, share=.7):
    import random
    rnd = random.Random(seed)
    falsy = [r[0] for r in conn.execute("SELECT id FROM task_events WHERE kind='created' "
                                        "AND (payload IS NULL OR payload='{}')")]
    marked = {i for i in falsy if rnd.random() < share}
    conn.executemany('INSERT INTO kanban_creator_empty_events VALUES (?)', [(i,) for i in marked])
    conn.commit()
    return marked


def traced(conn, fn):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        return fn(), statements
    finally:
        conn.set_trace_callback(None)


SHAPES = [(50, 0, 1), (50, 3, 2), (300, 3, 3), (300, 8, 4), (2000, 1, 5), (2000, 3, 6), (300, 0, 7)]


@pytest.mark.parametrize('analyze', [False, True])
@pytest.mark.parametrize('n,extra,seed', SHAPES)
def test_envelope_order_equals_literal_order_for_both_drivers(board, n, extra, seed, analyze):
    from hermes_cli import kanban_creator_empty as empty
    assert empty.ensure(board)
    populate(board, n, extra, seed)
    if analyze:
        board.execute('ANALYZE')
    literal4 = [tuple(r) for r in board.execute(ORIGINAL_ROWS)]
    literal3 = [tuple(r) for r in board.execute(PRE_PATCH)]
    assert [r[:3] for r in literal4] == literal3
    assert details(board, ORIGINAL_ROWS) == details(board, PRE_PATCH)
    marked = mark_falsy(board, seed)
    wanted = [r for r in literal4 if r[3] not in marked]
    assert [tuple(r) for r in board.execute(ENVELOPE)] == wanted
    assert [r[:3] for r in wanted] == [r for r, full in zip(literal3, literal4) if full[3] not in marked]
    plan = details(board, ENVELOPE)
    assert plan[0] == 'CO-ROUTINE c'
    assert plan[1:1 + len(details(board, PRE_PATCH))] == details(board, PRE_PATCH)
    got, statements = traced(board, lambda: empty.candidates(board))
    assert [tuple(r) for r in got] == wanted
    assert ENVELOPE in statements and ORIGINAL_ROWS not in statements  # the guard accepted this plan
    assert marked and len(wanted) < len(literal4)


def test_fixture_set_covers_scan_and_search_drivers(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch)
    drivers = set()
    for i, (n, extra, seed, analyze) in enumerate([(300, 3, 3, True), (50, 3, 2, True), (300, 3, 3, False),
                                                   (2000, 1, 5, False), (2000, 1, 5, True), (300, 0, 7, False)]):
        with kbc.connect_closing(db_path=tmp_path / f'drivers-{i}.db') as conn:  # fresh DB: no stale statistics
            populate(conn, n, extra, seed)
            if analyze:
                conn.execute('ANALYZE')
            drivers.add(details(conn, ORIGINAL_ROWS)[0].split(' USING')[0])
    assert {'SCAN t', 'SEARCH e'} <= drivers, drivers


@pytest.mark.parametrize('direction', ['before', 'after'])
def test_inheritance_chain_outcome_matches_unfiltered_literal_path(board, tmp_path, direction):
    """Same-pass ancestor visibility: parents earlier or later in literal order, versus the base path."""
    import shutil
    from hermes_cli import kanban_chat_links as links
    from hermes_cli import kanban_creator_empty as empty
    board.execute('BEGIN')
    ids = ['root', *[f'n{(i * 7) % 61:02}' for i in range(61)]]
    for tid in ids:
        board.execute("INSERT INTO tasks(id,title,assignee,status,created_at) VALUES (?,?,'default','todo',1)", (tid, tid))
        board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'comment','{}',1)", (tid,))
        board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'comment','{}',1)", (tid,))
        board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'comment','{}',1)", (tid,))
        if tid != 'root':
            board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'created','{}',1)", (tid,))
    board.execute("INSERT INTO task_chat_links(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                  "VALUES (?,'root','away','default','human','created',0,1)",
                  (links.link_uid('root', 'away', 'default', 'human', 'created'),))
    board.commit()
    board.execute('ANALYZE')
    order = [r['id'] for r in board.execute(ORIGINAL_ROWS)]
    truthy = order[1::2] if direction == 'before' else order[::2]
    falsy = [t for t in order if t not in truthy]
    chain = ['root', *truthy] if direction == 'before' else [*truthy[1:], 'root']
    pairs = zip(truthy, chain[:len(truthy)]) if direction == 'before' else zip(truthy, [*truthy[1:], 'root'])
    for tid, parent in pairs:
        board.execute('UPDATE task_events SET payload=? WHERE task_id=? AND kind=\'created\'',
                      (json.dumps({'creator_task_id': parent}), tid))
    board.commit()
    board.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    base_path = tmp_path / 'base.db'
    shutil.copy(path, base_path)
    base = kbc.connect(db_path=base_path)
    try:
        base.execute('CREATE TABLE kanban_creator_empty_events (future TEXT)')  # unknown layout: literal, unfiltered
        base.commit()
        assert empty.ensure(board)
        board.executemany('INSERT INTO kanban_creator_empty_events SELECT id FROM task_events WHERE task_id=? '
                          "AND kind='created'", [(t,) for t in falsy if t != 'root'])
        board.commit()
        got, base_got = creator.backfill(board), creator.backfill(base)
        dump = lambda c: sorted(tuple(r) for r in c.execute(
            'SELECT task_id,chat_gateway,chat_profile,chat_session,kind,released FROM task_chat_links'))
        assert got == base_got and got['attempted'] == truthy
        assert dump(board) == dump(base)
        assert got['unresolved'] != [] or direction == 'before'
        if direction == 'after':  # parents later in order are not yet linked: order really mattered
            assert got['unresolved'] != []
    finally:
        base.close()


def literal_plan_variant(monkeypatch, variant):
    """Rewrite the plans the guard sees, one condition at a time."""
    from hermes_cli import kanban_creator_empty as empty
    real = empty._plan

    def fake(conn, sql):
        plan = real(conn, sql)
        assert plan is not None
        if sql == empty._ENVELOPE:
            probe = [i for i, (_, x) in enumerate(plan) if x.startswith('SEARCH m')]
            scan_c = [i for i, (_, x) in enumerate(plan) if x == 'SCAN c']
            if variant == 'no_coroutine':
                return plan[1:]
            if variant == 'coroutine_renamed':
                return [(0, 'CO-ROUTINE x'), *plan[1:]]
            if variant == 'inner_changed':
                return [plan[0], (plan[1][0], 'SCAN zzz'), *plan[2:]]
            if variant == 'inner_depth':
                return [plan[0], *[(0, x) for _, x in plan[1:scan_c[0]]], *plan[scan_c[0]:]]
            if variant == 'inner_truncated':
                return plan[:2] + plan[scan_c[0]:]
            if variant == 'no_scan_c':
                return [p for p in plan if p[1] != 'SCAN c']
            if variant == 'scan_c_misplaced':
                return [plan[0], (0, 'SCAN c'), *plan[1:scan_c[0]], *plan[scan_c[0] + 1:]]
            if variant == 'scan_marker':
                return [*plan[:probe[0]], (0, 'SCAN m'), *plan[probe[0] + 1:]]
            if variant == 'no_probe':
                return [p for i, p in enumerate(plan) if i not in probe]
            if variant == 'probe_not_pk':
                return [*plan[:probe[0]], (0, 'SEARCH m USING INDEX x (a=?)'), *plan[probe[0] + 1:]]
            if variant == 'scan_c_dropped_tail':
                return plan[:scan_c[0] + 1]
            if variant == 'tail_missing':
                return plan[:scan_c[0]]
            extras = {'extra_scan_marker': 'SCAN m',
                      'temp_btree_in_tail': 'USE TEMP B-TREE FOR ORDER BY',
                      'extra_outer_scan': 'SCAN t', 'second_scan_c': 'SCAN c',
                      'materialize_in_tail': 'MATERIALIZE x',
                      'extra_search_m': 'SEARCH m USING INDEX zz'}
            if variant in extras:
                return [*plan, (0, extras[variant])]
            if variant == 'probe_depth0_no_subquery':
                return [*plan[:scan_c[0] + 1], (0, empty._PROBE)]
            if variant == 'empty_plans':  # an empty literal agreeing with an empty pre-patch plan proves nothing
                return [plan[0], *plan[scan_c[0]:]]
        if sql == empty._PRE_PATCH_ROWS and variant == 'prepatch_differs':
            return [(0, 'SCAN zzz'), *plan[1:]]
        if sql == empty._ROWS and variant == 'literal_differs':
            return [(0, 'SCAN zzz'), *plan[1:]]
        if variant == 'empty_plans' and sql in (empty._ROWS, empty._PRE_PATCH_ROWS):
            return []
        return plan

    monkeypatch.setattr(empty, '_plan', fake)


VARIANTS = ['no_coroutine', 'coroutine_renamed', 'inner_changed', 'inner_depth', 'inner_truncated', 'no_scan_c',
            'scan_c_misplaced', 'scan_marker', 'no_probe', 'probe_not_pk', 'scan_c_dropped_tail',
            'prepatch_differs', 'literal_differs', 'empty_plans',
            'tail_missing', 'extra_scan_marker', 'temp_btree_in_tail', 'extra_outer_scan',
            'second_scan_c', 'materialize_in_tail', 'extra_search_m', 'probe_depth0_no_subquery']


def marked_board(board, n=300):
    from hermes_cli import kanban_creator_empty as empty
    assert empty.ensure(board)
    populate(board, n, 3, 11)
    board.execute('ANALYZE')
    marked = mark_falsy(board, 11)
    literal = [tuple(r) for r in board.execute(ORIGINAL_ROWS)]
    return literal, [r for r in literal if r[3] not in marked]


@pytest.mark.parametrize('shape', ['orphan_parent', 'three_columns'])
@pytest.mark.parametrize('query', ['_ROWS', '_ENVELOPE', '_PRE_PATCH_ROWS'])
def test_malformed_eqp_falls_back_to_python_filter(board, shape, query):
    from hermes_cli import kanban_creator_empty as empty
    _, wanted = marked_board(board)

    class MalformedPlan:
        def __getattr__(self, name):
            return getattr(board, name)

        def execute(self, sql, *args):
            result = board.execute(sql, *args)
            if sql == 'EXPLAIN QUERY PLAN ' + getattr(empty, query):
                rows = [tuple(r) for r in result]
                node, parent, unused, detail = rows[0]
                rows[0] = ((node, 99999, unused, detail) if shape == 'orphan_parent'
                           else (node, parent, detail))
                return rows
            return result

    changes = board.total_changes
    got, statements = traced(board, lambda: empty.candidates(MalformedPlan()))
    assert [tuple(r) for r in got] == wanted
    assert ENVELOPE not in statements and statements.count(ORIGINAL_ROWS) == 1
    assert 'SELECT event_id FROM kanban_creator_empty_events' in statements
    assert board.total_changes == changes and not board.in_transaction


@pytest.mark.parametrize('analyzed', [False, True])
def test_guard_accepts_the_real_envelope_plan(board, analyzed):
    from hermes_cli import kanban_creator_empty as empty
    assert empty.ensure(board)
    populate(board, 300, 3, 11)
    if analyzed:
        board.execute('ANALYZE')
    assert empty._envelope_plan_ok(board) is True


@pytest.mark.parametrize('variant', VARIANTS)
def test_plan_guard_mismatch_falls_back_to_python_filter(board, monkeypatch, variant):
    from hermes_cli import kanban_creator_empty as empty
    _, wanted = marked_board(board)
    literal_plan_variant(monkeypatch, variant)
    assert empty._envelope_plan_ok(board) is False
    changes = board.total_changes
    got, statements = traced(board, lambda: empty.candidates(board))
    assert [tuple(r) for r in got] == wanted
    assert ENVELOPE not in statements and statements.count(ORIGINAL_ROWS) == 1
    assert 'SELECT event_id FROM kanban_creator_empty_events' in statements
    assert board.total_changes == changes and not board.in_transaction


def test_unflattened_limit_barrier_is_what_the_guard_relies_on(board, monkeypatch):
    """A SQLite that flattens the subquery (modelled by dropping LIMIT) is detected per call."""
    from hermes_cli import kanban_creator_empty as empty
    _, wanted = marked_board(board)
    flattened = ENVELOPE.replace(' LIMIT -1 OFFSET 0', '')
    assert details(board, flattened)[0] != 'CO-ROUTINE c'
    monkeypatch.setattr(empty, '_ENVELOPE', flattened)
    got, statements = traced(board, lambda: empty.candidates(board))
    assert [tuple(r) for r in got] == wanted and flattened not in statements


def test_guard_plan_is_recomputed_on_every_call(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    _, wanted = marked_board(board)
    calls = []
    real = empty._plan
    monkeypatch.setattr(empty, '_plan', lambda conn, sql: calls.append(sql) or real(conn, sql))
    for _ in range(3):
        assert [tuple(r) for r in empty.candidates(board)] == wanted
    assert calls == [empty._ROWS, empty._ENVELOPE, empty._PRE_PATCH_ROWS] * 3


def breakers():
    from hermes_cli import kanban_creator_empty as empty

    def drop_trigger(conn):
        conn.execute('DROP TRIGGER creator_empty_update')

    def change_trigger(conn):
        conn.execute('DROP TRIGGER creator_empty_delete')
        conn.execute('CREATE TRIGGER creator_empty_delete AFTER DELETE ON task_events '
                     'BEGIN DELETE FROM kanban_creator_empty_events WHERE event_id=OLD.id+0; END')

    def drop_table(conn):
        conn.execute('DROP TABLE kanban_creator_empty_events')

    def alien_layout(conn):
        conn.execute('DROP TABLE kanban_creator_empty_events')
        conn.execute('CREATE TABLE kanban_creator_empty_events (event_id INTEGER PRIMARY KEY, extra TEXT)')

    return {'missing_trigger': drop_trigger, 'mismatched_trigger_sql': change_trigger,
            'missing_marker_table': drop_table, 'unknown_table_layout': alien_layout}


@pytest.mark.parametrize('gap', ['missing_trigger', 'mismatched_trigger_sql', 'missing_marker_table',
                                 'unknown_table_layout'])
def test_coverage_gap_returns_unfiltered_literal_rows_without_writes(board, monkeypatch, gap):
    from hermes_cli import kanban_creator_empty as empty
    literal, _ = marked_board(board, 120)
    breakers()[gap](board)
    board.commit()
    monkeypatch.setattr(empty, 'ensure', lambda conn: True)  # the gap opens after ensure, as a peer could
    plans = []
    monkeypatch.setattr(empty, '_plan', lambda *a: plans.append(a))
    cookie = board.execute('PRAGMA schema_version').fetchone()[0]
    changes = board.total_changes
    got, statements = traced(board, lambda: empty.candidates(board))
    assert [tuple(r) for r in got] == literal
    assert ENVELOPE not in statements and plans == []
    assert board.total_changes == changes
    assert board.execute('PRAGMA schema_version').fetchone()[0] == cookie and not board.in_transaction


def test_caller_owned_transaction_uses_literal_unfiltered_without_ddl_or_envelope(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    literal, _ = marked_board(board, 120)
    plans = []
    monkeypatch.setattr(empty, '_plan', lambda *a: plans.append(a))
    board.execute('BEGIN')
    try:
        cookie = board.execute('PRAGMA schema_version').fetchone()[0]
        got, statements = traced(board, lambda: empty.candidates(board))
        assert [tuple(r) for r in got] == literal
        assert not any(s.startswith(('CREATE', 'DROP', 'DELETE', 'INSERT', 'COMMIT', 'ROLLBACK'))
                       for s in statements) and ENVELOPE not in statements
        assert plans == []
        assert board.in_transaction
        assert board.execute('PRAGMA schema_version').fetchone()[0] == cookie
    finally:
        board.rollback()


def peer(board):
    path = board.execute('PRAGMA database_list').fetchone()[2]
    return sqlite3.connect(path, isolation_level=None)


def test_peer_commit_before_snapshot_unhides_changed_marked_event(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)  # event 123 certified
    real = empty.ensure

    def ensure_then_peer(conn):
        ok = real(conn)
        p = peer(conn)
        try:
            p.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"raced"}',))
        finally:
            p.close()
        return ok

    monkeypatch.setattr(empty, 'ensure', ensure_then_peer)
    got, statements = traced(board, lambda: empty.candidates(board))
    assert [r['creator_event_id'] for r in got] == [123] and ENVELOPE in statements


def test_peer_commit_after_snapshot_is_invisible_to_the_same_statement_then_seen(board, monkeypatch):
    from hermes_cli import kanban_creator_empty as empty
    seed(board, '{}')
    creator.backfill(board)
    real, fired = empty._schema, []

    def schema_then_peer(conn):
        schema = real(conn)
        if conn.in_transaction and not fired:
            fired.append(True)
            p = peer(conn)
            try:
                p.execute('UPDATE task_events SET payload=? WHERE id=123', ('{"creator_task_id":"late"}',))
            finally:
                p.close()
        return schema

    monkeypatch.setattr(empty, '_schema', schema_then_peer)
    assert empty.candidates(board) == []  # marker and payload are read from one snapshot
    assert fired and not board.in_transaction
    assert [r['creator_event_id'] for r in empty.candidates(board)] == [123]  # committed change never stays hidden


def test_truthy_unresolved_rows_retry_every_pass_and_humans_are_never_marked(board, monkeypatch):
    from hermes_cli import kanban_db_notify as notify
    board.execute("INSERT INTO tasks (id,title,assignee,status,created_at,session_id) VALUES "
                  "('falsy','f','default','todo',1,NULL),('truthy','t','default','todo',1,NULL),"
                  "('human','h','default','todo',1,'human')")
    board.executemany("INSERT INTO task_events (id,task_id,kind,payload,created_at) VALUES (?,?,'created',?,1)",
                      [(1, 'falsy', '{}'), (2, 'truthy', '{"creator_task_id":"p"}'), (3, 'human', 'broken')])
    board.commit()
    monkeypatch.setattr(notify, 'headless_sessions', lambda sessions: set())
    inherited = []
    monkeypatch.setattr(creator, 'inherit_chats', lambda conn, tid, parent, **kw: inherited.append(tid))
    for _ in range(3):
        assert creator.backfill(board) == {'attempted': ['truthy'], 'unresolved': ['truthy']}
    assert inherited == ['truthy'] * 3
    assert [r[0] for r in board.execute('SELECT event_id FROM kanban_creator_empty_events')] == [1]


class Failing:
    """Raise a chosen SQLite error at one statement; everything else is the real connection."""
    def __init__(self, conn, match, code, message='injected'):
        self.conn, self.match, self.code, self.message = conn, match, code, message

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if self.match(self.conn, sql):
            error = sqlite3.DatabaseError(self.message)
            error.sqlite_errorcode = self.code
            raise error
        return self.conn.execute(sql, *args)


STAGES = {
    'schema': lambda c, s: c.in_transaction and 'FROM sqlite_master' in s,
    'plan': lambda c, s: s.startswith('EXPLAIN QUERY PLAN'),
    'envelope': lambda c, s: s == ENVELOPE,
    'marker': lambda c, s: s.startswith('SELECT event_id FROM'),
}


@pytest.mark.parametrize('stage', list(STAGES))
@pytest.mark.parametrize('code', [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_IOERR,
                                  sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_FULL, sqlite3.SQLITE_CORRUPT | (1 << 8),
                                  sqlite3.SQLITE_IOERR | (2 << 8)])
def test_serious_sqlite_errors_propagate_and_release_the_snapshot(board, monkeypatch, stage, code):
    from hermes_cli import kanban_creator_empty as empty
    marked_board(board, 60)
    if stage == 'marker':
        monkeypatch.setattr(empty, '_envelope_plan_ok', lambda conn: False)
    with pytest.raises(sqlite3.DatabaseError, match='injected'):
        empty.candidates(Failing(board, STAGES[stage], code))
    assert not board.in_transaction
    assert board.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 60


@pytest.mark.parametrize('stage', list(STAGES))
@pytest.mark.parametrize('code,message', [(sqlite3.SQLITE_AUTH, 'not authorized'),
                                          (sqlite3.SQLITE_ERROR, 'no such table: kanban_creator_empty_events'),
                                          (sqlite3.SQLITE_BUSY, 'database is locked')])
def test_harmless_sqlite_refusals_fall_back_to_the_unfiltered_literal(board, monkeypatch, stage, code, message):
    from hermes_cli import kanban_creator_empty as empty
    literal, _wanted = marked_board(board, 60)
    if stage == 'marker':
        monkeypatch.setattr(empty, '_envelope_plan_ok', lambda conn: False)
    got = empty.candidates(Failing(board, STAGES[stage], code, message))
    assert [tuple(r) for r in got] == literal
    assert not board.in_transaction
