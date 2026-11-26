"""Atomic canonical effect import plus private APPLIED, never task resurrection.

The certificate task is HISTORICAL and is deliberately not imported. Ordinary
current feed/claim/private hold gates still decide execution, including successors.
"""
from __future__ import annotations

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_final as final, kanban_release_replica as replica

TABLE = 'owner_release_canonical_applications'


def install(conn):
    app.install(conn)
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY, evidence TEXT NOT NULL,digest TEXT NOT NULL)')
    abort = "BEGIN SELECT RAISE(ABORT,'canonical application certificate immutable'); END"
    for op in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_owner_release_application_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR rowid=NEW.rowid) {abort}')


def verified(conn, intent_uid):
    """Private historical response; unavailable halves never reconstruct authority."""
    import sqlite3
    try:
        row = conn.execute(f'SELECT evidence,digest FROM {TABLE} WHERE intent_uid=? AND typeof(evidence)=\'text\' '
                           'AND length(CAST(evidence AS BLOB))<=?', (intent_uid, final.MAX_BYTES)).fetchone()
        if row is None:
            return None
        value = cert.parse(row[0], max_bytes=final.MAX_BYTES)
        wire = final.saved(conn, intent_uid)
        if wire is None or app._sha(value) != row[1]:
            return None
        return final.validate(value, wire)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, sqlite3.Error):
        return None


def _no_pending(conn, changes):
    # A private certificate is not permission to discard local issued/pending
    # transport, even when public logical fields look equal. Normal sync owns it.
    for change in changes:
        if conn.execute('SELECT 1 FROM kanban_sync_outbox WHERE table_name=? AND row_uid=? LIMIT 1',
                        (change.table, change.uid)).fetchone():
            raise ValueError('application row has unanswered local transport')


def materialize(conn, value):
    """Authenticated GET caller only; ONE ordinary remote-apply writer, no HTTP.

    A final keep callback is a private sentinel after all canonical rows. It runs
    in apply_remote_changes' reservation, verifies actual materialization, then
    records private certification and APPLIED last. It is not a second/nested txn.
    """
    from hermes_cli import kanban_db_sync as sync, kanban_sync_client as client
    if conn.in_transaction:
        raise ValueError('application materialization owns its remote-apply writer')
    uid = value['operation']['intent_uid']
    wire = final.saved(conn, uid)
    if wire is None:
        raise ValueError('application has no frozen final wire')
    value = final.validate(value, wire)
    proof = replica.verified_consumption(conn, value['consumption']['consume']['client_op_id'])
    if proof is None or cert.encode(proof) != cert.encode(value['consumption']):
        raise ValueError('application winning private consumption missing')
    install(conn)
    prior = app.verified_application(conn, uid)
    if prior is not None:
        if cert.encode(verified(conn, uid)) != cert.encode(value):
            raise ValueError('application replay private certification differs')
        return prior
    # Comments first so portable UID references resolve despite remapped IDs.
    changes = [client._row_change(table, row) for table, rows in
               (('task_comments', value['comments']), ('task_events', value['events'])) for row in rows]
    sentinel = client._row_change('task_events', value['events'][-1])
    cursor = sync.sync_state(conn)['cursor']
    before = None
    visited = 0

    def keep(current, change):
        nonlocal before, visited
        if visited == 0:
            if sync.sync_state(current)['cursor'] != cursor:
                raise ValueError('application feed cursor advanced before materialization')
            if cert.encode(final.saved(current, uid)) != cert.encode(wire):
                raise ValueError('application frozen wire changed under writer')
            _no_pending(current, changes)
            before = tuple(current.execute('SELECT * FROM tasks WHERE id=?', (wire['task'],)).fetchone())
        visited += 1
        if change is not sentinel:
            return True
        # The historical task postimage must never overwrite a successor or local
        # claim/pending status. All current fields remain byte-for-byte unchanged.
        if tuple(current.execute('SELECT * FROM tasks WHERE id=?', (wire['task'],)).fetchone()) != before:
            raise ValueError('application import changed current task state')
        for e in value['events']:
            if cert.encode(app._event(current, e['uid'])) != cert.encode({k:e[k] for k in app.EVENT_FIELDS}):
                raise ValueError('canonical application event failed materialization')
        for c in value['comments']:
            row = current.execute('SELECT uid,task_id,author,body,created_at,origin_gateway FROM task_comments WHERE uid=?', (c['uid'],)).fetchone()
            if row is None or cert.encode(list(row)) != cert.encode([c[k] for k in ('uid','task_id','author','body','created_at','origin_gateway')]):
                raise ValueError('canonical application comment failed materialization')
        token = app._PERMIT.set(current)
        try:
            raw, digest = cert.encode(value), app._sha(value)
            old = current.execute(f'SELECT evidence,digest FROM {TABLE} WHERE intent_uid=?', (uid,)).fetchone()
            if old is not None and tuple(old) != (raw, digest):
                raise ValueError('canonical application private cache conflict')
            if old is None:
                current.execute(f'INSERT INTO {TABLE} VALUES (?,?,?)', (uid, raw, digest))
            app._record(current, consume_uid=proof['consume']['client_op_id'],
                        delivery_uid=wire['proposal']['delivery_uid'], result_uid=value['events'][-1]['uid'])
            if app.verified_application(current, uid) is None:
                raise ValueError('canonical application APPLIED verification failed')
        finally:
            app._PERMIT.reset(token)
        return False

    sync.apply_remote_changes(conn, [*changes, sentinel], new_cursor=cursor, board=proof['intent']['board'],
                              observers=False, local_pending_wins=True, keep=keep)
    if visited != len(changes) + 1 or app.verified_application(conn, uid) is None:
        raise ValueError('canonical application materialization incomplete')
    return app.verified_application(conn, uid)
