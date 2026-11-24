"""Read-only custody of an exact home-local Desktop operation. [daveey]

The operation ID is a lookup hint, never proof. Resolve source from the runtime,
keep its read transaction alive through issuance/consumption, and bind immutable
producer fields. Same-user filesystem compromise remains outside this boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3

from hermes_cli import kanban_selfwork_exception as sx


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@contextmanager
def custody(operation_id, *, delivered=False):
    """No path, profile, session or proof is accepted from the invoking caller."""
    from hermes_cli.kanban_card_answer_runtime import _profile
    from hermes_cli.install_identity import get_install_id
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', operation_id):
        raise sx.Refused('local_operation_id_invalid')
    profile, home = _profile()
    install = get_install_id()
    if not install:
        raise sx.Refused('local_install_unavailable')
    namespace = json.dumps(['install:' + install, profile], separators=(',', ':'), ensure_ascii=False)
    path = home / 'user-steps/actions.sqlite3'
    source = None
    try:
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)  # noqa: kanban-lockguard
        source.row_factory = sqlite3.Row
        source.execute('BEGIN')
        evidence = _read(source, namespace, operation_id, profile, install, delivered)
    except sx.Refused:
        if source is not None:
            source.close()
        raise
    except sqlite3.OperationalError as exc:
        if source is not None:
            source.close()
        if any(word in str(exc).lower() for word in ('locked', 'busy')):
            raise OSError('local_source_busy') from None
        raise sx.Refused('local_source_unavailable') from None
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError, AttributeError, IndexError, UnicodeError):
        if source is not None:
            source.close()
        raise sx.Refused('local_source_malformed') from None
    try:
        yield evidence
    finally:
        source.close()


def _read(source, namespace, oid, profile, install, delivered):
    rows = source.execute('SELECT * FROM operations WHERE namespace=? AND id=?', (namespace, oid)).fetchall()
    if len(rows) != 1:
        raise sx.Refused('local_operation_missing_or_ambiguous')
    row = rows[0]
    op = sx._strict(row['body'].encode())
    owner = dict(gatewayId='install:' + install, profile=profile, storedSessionId=row['session'])
    if (op.get('owner') != owner or op.get('operationId') != oid or not row['session']
            or not isinstance(op.get('recordedAt'), str) or not op['recordedAt']
            or op.get('dispatchState') not in ('pending', 'failed', 'delivered') or op.get('retired')
            or (delivered and op.get('dispatchState') != 'delivered')):
        raise sx.Refused('local_operation_mismatch')
    bound = source.execute('SELECT * FROM kanban_projections WHERE namespace=? AND session=? AND request_id=?',
                           (namespace, row['session'], op['requestId'])).fetchall()
    if len(bound) != 1:
        raise sx.Refused('local_projection_missing_or_ambiguous')
    binding = dict(bound[0])
    snapshot = sx._strict(binding['snapshot'].encode())
    frozen = sx._strict(binding['request'].encode())
    intent, decision = op['intent'], op['decision']
    if (binding['board'] != 'default' or binding['task'] != sx.TARGET.owner_task
            or snapshot.get('task') != sx.TARGET.owner_task or snapshot.get('session') != row['session']
            or set(snapshot) - {'task', 'session', 'generation', 'ask', 'title'}
            or type(snapshot.get('generation')) is not int or not isinstance(snapshot.get('ask'), dict)
            or snapshot['ask'].get('choices') or 'release_target' in snapshot['ask']):
        raise sx.Refused('local_projection_target_mismatch')
    choices = [dict(effect='answer', id='card-0', label='Unblock'),
               dict(effect='answer', id='card-1', label='Archive')]
    if (intent.get('request') != frozen or frozen.get('owner') != owner or frozen.get('choices') != choices
            or type(frozen.get('revision')) is not int or frozen['revision'] < 1
            or op.get('revision') != frozen['revision'] or decision.get('expectedRevision') != frozen['revision']
            or op['requestId'] != frozen.get('requestId') or decision.get('requestId') != frozen['requestId']
            or decision.get('choiceId') != 'card-0' or intent.get('effect') != 'answer'
            or intent.get('revisionText') != 'Unblock' or decision.get('revisionText') != 'Unblock'
            or intent.get('executionAuthorized') is not False or intent.get('permissionGrants') != []
            or op.get('idempotencyKey') != decision.get('idempotencyKey') or op.get('provenance')):
        raise sx.Refused('local_operation_intent_mismatch')
    current = source.execute('SELECT * FROM requests WHERE namespace=? AND session=? AND id=?',
                             (namespace, row['session'], op['requestId'])).fetchone()
    revision = source.execute('SELECT body FROM revisions WHERE namespace=? AND id=? AND revision=?',
                              (namespace, op['requestId'], op['revision'])).fetchone()
    resolution = dict(choiceId='card-0', operationId=oid, revision=op['revision'], recordedAt=op['recordedAt'])
    if (current is None or current['revision'] != op['revision'] or current['state'] == 'withdrawn'
            or sx._strict((current['resolution'] or 'null').encode()) != resolution
            or revision is None or sx._strict(revision[0].encode()) != frozen):
        raise sx.Refused('local_request_revision_changed')
    receipt = dict(destination='kanban_ask', board='default', task=sx.TARGET.owner_task, operationId=oid,
                   value='unblock', executionStarted=False, fingerprint=digest([intent, decision]))
    if delivered and op.get('receipt') != receipt:
        raise sx.Refused('local_operation_receipt_mismatch')
    immutable = {k: op.get(k) for k in ('operationId', 'owner', 'requestId', 'revision', 'idempotencyKey',
                                       'decision', 'recordedAt', 'intent', 'provenance')}
    return dict(operation=op, snapshot=snapshot, receipt=receipt,
                binding_sha256=digest({'operation': immutable, 'projection': binding}),
                source=dict(install=install, profile=profile, session=row['session'],
                            requestId=op['requestId'], revision=op['revision']))
