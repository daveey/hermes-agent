"""Frozen real-assembly proposals. Trial effects ALWAYS roll back before commit.

A proposal is historical local planning custody, not current canonical authority,
APPLIED proof, dispatch clearance, or permission to replay ordinary row operations.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_replica as replica
from hermes_cli.kanban_containment_functions import register as register_function

TABLE = 'owner_release_prepared'
_FIELDS = {'version', 'consumption', 'delivery_uid', 'task_before', 'task_after', 'events', 'comments', 'application'}


def install(conn):
    register_function(conn, 'kanban_owner_release_prepare_write', 0,
                      lambda: int(app._PERMIT.get() is conn and app._INVOCATION.get() is not None and conn.in_transaction))
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY, consume_uid TEXT NOT NULL UNIQUE, '
                 'delivery_uid TEXT NOT NULL UNIQUE, board TEXT NOT NULL, task_id TEXT NOT NULL, '
                 'evidence TEXT NOT NULL, digest TEXT NOT NULL)')
    abort = "BEGIN SELECT RAISE(ABORT,'owner release prepared proposal is immutable'); END"
    for op in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_owner_release_prepare_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR consume_uid=NEW.consume_uid '
                 f'OR delivery_uid=NEW.delivery_uid OR rowid=NEW.rowid) {abort}')


def _validate(data):
    version = data.get('version') if isinstance(data, dict) else None
    if (type(version) is not int or version not in (1, 2)
            or not cert._exact(data, _FIELDS | ({'accounting'} if version == 2 else set()))):
        raise ValueError('prepared proposal shape differs')
    if version == 2:
        accounting = data['accounting']
        if (not cert._exact(accounting, {'semantics', 'spent_runs'})
                or accounting['semantics'] != 'core_home_ordinary_v1'
                or type(accounting['spent_runs']) is not int
                or not 0 <= accounting['spent_runs'] <= 9007199254740991):
            raise ValueError('prepared proposal accounting differs')
    proof = cert.parse(cert.encode(data['consumption']))
    body, delivery = proof['intent'], data['delivery_uid']
    cert.validate(proof, board=body['board'], task=body['task_id'], intent_uid=body['intent_uid'])
    if not cert.intent._coordinate(delivery) or delivery in (body['intent_uid'], proof['consume']['client_op_id']):
        raise ValueError('prepared delivery differs')
    before, after = data['task_before'], data['task_after']
    if (not isinstance(before, dict) or not isinstance(after, dict) or before.get('id') != body['task_id']
            or before.get('status') != 'triage' or after.get('status') != 'ready'
            or cert.encode({**before, 'status': 'ready'}) != cert.encode(after)):
        raise ValueError('prepared task delta is not the real bounded promotion')
    application = data['application']
    if (not isinstance(application, dict) or application.get('delivery_uid') != delivery
            or cert.encode(application.get('consumption')) != cert.encode(proof)
            or cert.encode(application.get('intent')) != cert.encode(body)):
        raise ValueError('prepared application differs')
    events, comments = data['events'], data['comments']
    if (not isinstance(events, list) or not 3 <= len(events) <= 38
            or not isinstance(comments, list) or len(comments) > 1):
        raise ValueError('prepared generated row bounds differ')
    uids = []
    for row in [*events, *comments]:
        if (not isinstance(row, dict) or row.get('task_id') != body['task_id']
                or not cert.intent._coordinate(row.get('uid'))):
            raise ValueError('prepared generated row identity differs')
        uids.append(row['uid'])
    if len(uids) != len(set(uids)):
        raise ValueError('prepared generated row identities collide')
    frozen = {e['uid']: e for e in events}
    for event in [application['result_event'], *application['effects']]:
        if event['uid'] not in frozen or cert.encode({k:frozen[event['uid']].get(k) for k in app.EVENT_FIELDS}) != cert.encode(event):
            raise ValueError('prepared effect image differs')
    result = app._result(application['result_event'], proof, delivery)
    if ([e['uid'] for e in application['effects']] != result['effect_uids']
            or cert.encode(result) != cert.encode(application['result'])):
        raise ValueError('prepared result differs')
    app._effects(application['effects'], proof)
    kinds = [e.get('kind') for e in events]
    if sorted(k for k in kinds if k != 'wake_episode_evidence') != sorted([e['kind'] for e in application['effects']] + ['owner_release_applied'] + ['commented'] * len(comments)) or kinds.count('wake_episode_evidence') > 1:
        raise ValueError('prepared generated event inventory differs: ' + repr(kinds))
    budgets = [cert.parse(e['payload']) for e in events if e.get('kind') == 'spend_budget_set']
    if len(budgets) != len(comments):
        raise ValueError('prepared budget comment inventory differs')
    if comments:
        comment, decision = comments[0], budgets[0].get('decision_comment', {})
        if (not isinstance(decision, dict) or comment.get('author') != 'kanban-spend-budget' or decision.get('comment_uid') != comment['uid']
                or type(comment.get('id')) is not int or decision.get('comment_id') != comment['id']):
            raise ValueError('prepared portable budget comment differs')
    return data


def verified_prepared(conn, intent_uid):
    """SQL-only immutable proposal read; never compare rolled-back proposed rows.

    Current owner/task/episodes can change without altering historical proposal
    identity. Canonical application MUST revalidate them, never trust this read.
    """
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = conn.execute(f'SELECT intent_uid,consume_uid,delivery_uid,board,task_id,evidence,digest FROM {TABLE} '
                           "WHERE intent_uid=? AND typeof(evidence)='text' AND length(CAST(evidence AS BLOB))<=?",
                           (intent_uid, cert.MAX_BYTES)).fetchone()
        if row is None or row[3] != replica._board(conn):
            return None
        data = _validate(cert.parse(row[5]))
        body = data['consumption']['intent']
        if ((row[0], row[1], row[2], row[3], row[4]) != (body['intent_uid'], data['consumption']['consume']['client_op_id'],
                data['delivery_uid'], body['board'], body['task_id']) or row[6] != app._sha(data)):
            return None
        return data
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError, sqlite3.Error):
        return None


@dataclass(frozen=True, slots=True)
class _Trial:
    conn: object
    invocation: object
    raw: str


def _observe(conn, proof, delivery, *, application_version=1):
    from hermes_cli import kanban_release_assemble as assemble
    if app._PERMIT.get() is not conn or app._INVOCATION.get() is None or not conn.in_transaction:
        raise ValueError('proposal trial requires its unique application writer')
    tid = proof['intent']['task_id']
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    marks = {table:conn.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0]
             for table in ('task_events', 'task_comments')}
    conn.execute('SAVEPOINT owner_release_proposal_trial')
    observed = []
    token = assemble._BUDGET_OBSERVATION.set(observed if application_version == 2 else None)
    try:
        assemble._compose(conn, proof, delivery)
        application = app.verified_application(conn, proof['intent']['intent_uid'])
        if application is None:
            raise ValueError('trial failed to produce actual application proof')
        data = dict(version=application_version, consumption=proof, delivery_uid=delivery, task_before=before,
                    task_after=dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()),
                    events=[dict(r) for r in conn.execute('SELECT * FROM task_events WHERE id>? ORDER BY id', (marks['task_events'],))],
                    comments=[dict(r) for r in conn.execute('SELECT * FROM task_comments WHERE id>? ORDER BY id', (marks['task_comments'],))],
                    application=application)
        if application_version == 2:
            if len(observed) != 1:
                raise ValueError('proposal requires exactly one real budget observation')
            data['accounting'] = dict(semantics='core_home_ordinary_v1', spent_runs=observed[0])
        raw = cert.encode(_validate(data))
        cert.parse(raw)  # whole-envelope cap, not merely per-row limits
    finally:
        assemble._BUDGET_OBSERVATION.reset(token)
        conn.execute('ROLLBACK TO owner_release_proposal_trial')
        conn.execute('RELEASE owner_release_proposal_trial')
    return _Trial(conn, app._INVOCATION.get(), raw)


def _store(conn, trial):
    if (type(trial) is not _Trial or trial.conn is not conn or trial.invocation is not app._INVOCATION.get()
            or app._PERMIT.get() is not conn or not conn.in_transaction):
        raise ValueError('proposal observation is not bound to this writer')
    data = _validate(cert.parse(trial.raw))
    body = data['consumption']['intent']
    conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?)',
                 (body['intent_uid'], data['consumption']['consume']['client_op_id'], data['delivery_uid'],
                  body['board'], body['task_id'], trial.raw, app._sha(data)))


def _replay(conn, proof, delivery):
    prior = verified_prepared(conn, proof['intent']['intent_uid'])
    if prior is not None:
        if prior['delivery_uid'] != delivery or cert.encode(prior['consumption']) != cert.encode(proof):
            raise ValueError('prepared proposal replay differs')
        return prior
    if conn.execute(f'SELECT 1 FROM {TABLE} WHERE intent_uid=? OR consume_uid=? OR delivery_uid=?',
                    (proof['intent']['intent_uid'], proof['consume']['client_op_id'], delivery)).fetchone():
        raise ValueError('prepared proposal custody is corrupt; no new trial')
    return None


def prepare(conn, proof, *, delivery_uid, application_version=1):
    """Retain only an immutable proposal; real trial effects never survive commit."""
    if type(application_version) is not int or application_version not in (1, 2):
        raise ValueError('unsupported application version')
    install(conn)
    prior = _replay(conn, proof, delivery_uid)
    if prior is not None:
        return prior
    app.install(conn)
    with app._writer(conn):
        prior = _replay(conn, proof, delivery_uid)
        if prior is not None:
            return prior
        if app.verified_application(conn, proof['intent']['intent_uid']) is not None:
            raise ValueError('existing application cannot be converted into a proposal')
        trial = (_observe(conn, proof, delivery_uid) if application_version == 1 else
                 _observe(conn, proof, delivery_uid, application_version=application_version))
        _store(conn, trial)
    return verified_prepared(conn, proof['intent']['intent_uid'])
