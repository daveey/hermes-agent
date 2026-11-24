"""Exact home-local answer issuance and append-only recovery. [daveey]

Never relabel a local operation as a remote proof. The old receipt is immutable;
usable authority is independently revalidated at intake, not inferred from replay.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli import kanban_selfwork_local_source as source

VERSION = 1
RECOVERED = 'selfwork_exception_local_recovered'
RECOVERY_OPERATION = '51d35f6a-c966-4d0e-abeb-c80721803ba6'


def _uid(conn, event):
    row = conn.execute('SELECT uid FROM task_events WHERE id=?', (event['id'],)).fetchone()
    if row is None or not row[0]:
        raise sx.Refused('local_event_identity_missing')
    return row[0]


def _offer(conn, evidence):
    ask = evidence['snapshot']['ask']
    stamp = ask.get(sx.STAMP)
    offers = sx._own(conn, sx.OFFERED)
    if (not sx._bound(stamp) or stamp.get('scope') != 'accounting-recovery-only' or not offers
            or offers[0]['payload'].get(sx.STAMP) != stamp
            or offers[0]['payload'].get('ask_sha256') != source.digest(ask)):
        raise sx.Refused('local_offer_replaced_or_invalid')
    return offers[0], stamp


def eligible(conn, evidence, *, now=None):
    """Re-run under the board writer at both issuance and consumption."""
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_spend_decision import pending
    from hermes_cli.kanban_refusal_receipt import restricted
    sx._home(conn)
    offer, stamp = _offer(conn, evidence)
    now = int(time.time()) if now is None else now
    row = conn.execute('SELECT status,human_request FROM tasks WHERE id=?', (sx.TARGET.owner_task,)).fetchone()
    if (row is None or row['status'] in ('archived', 'done') or pending(conn, sx.TARGET.owner_task)
            or contained(conn, sx.TARGET.owner_task) or restricted(conn, sx.TARGET.owner_task)):
        raise sx.Refused('local_owner_held_or_closed')
    if now >= stamp['expires_at'] or sx._spent(conn, stamp['offer_uid']):
        raise sx.Refused('local_offer_expired_or_spent')
    if any(e['id'] > offer['id'] for e in sx._own(conn, 'archived')):
        raise sx.Refused('local_offer_declined')
    ask = json.loads(row['human_request'] or 'null')
    if ask is not None and ask != evidence['snapshot']['ask']:
        raise sx.Refused('local_ask_replaced')
    return stamp


def _prior_unblock(conn, evidence, offer=None):
    if offer is None:
        offer, _ = _offer(conn, evidence)
    oid = evidence['operation']['operationId']
    matches = [e for e in sx._own(conn, 'unblocked')
               if e['payload'].get('user_steps_projection', {}).get('operationId') == oid]
    if len(matches) != 1 or matches[0]['id'] <= offer['id']:
        raise sx.Refused('local_unblock_missing_or_ambiguous')
    event = matches[0]
    if event['payload'].get('user_steps_projection') != evidence['receipt']:
        raise sx.Refused('local_unblock_receipt_changed')
    body = 'Choice recorded in Desktop: ' + source.encoded(dict(label='Unblock', value='unblock')) + '\nOperation: ' + oid
    comments = conn.execute('SELECT id FROM task_comments WHERE task_id=? AND author=? AND body=?',
                            (sx.TARGET.owner_task, 'user-steps', body)).fetchall()
    if len(comments) != 1:
        raise sx.Refused('local_decision_comment_missing_or_ambiguous')
    # Require a home-owned comment event linking the actual immutable decision.
    comment_events = [e for e in sx._own(conn, 'commented')
                      if e['payload'].get('comment_id') == comments[0][0]]
    if (len(comment_events) != 1 or not offer['id'] < comment_events[0]['id'] < event['id']):
        raise sx.Refused('local_comment_event_invalid')
    return event


def _grant(evidence, stamp, event_uid):
    return dict(offer_uid=stamp['offer_uid'], stamp=stamp,
                operationId=evidence['operation']['operationId'], local_version=VERSION,
                binding_sha256=evidence['binding_sha256'], source=evidence['source'],
                fingerprint=evidence['receipt']['fingerprint'], unblock_event_uid=event_uid)


def recorded(conn, offer_uid):
    """Validated historical receipt, not permission to consume it again."""
    events = [e for kind in (RECOVERED, 'unblocked') for e in sx._own(conn, kind)
              if e['payload'].get(sx.GRANT, {}).get('offer_uid') == offer_uid
              and e['payload'][sx.GRANT].get('local_version') == VERSION]
    if not events:
        return None
    if len(events) != 1:
        raise sx.Refused('local_grant_ambiguous')
    grant = events[0]['payload'][sx.GRANT]
    recovered = events[0]['kind'] == RECOVERED
    with source.custody(grant.get('operationId'), delivered=recovered) as evidence:
        # Historical receipt validation does not make expired/spent authority usable.
        old_offer = [e for e in sx._own(conn, sx.OFFERED)
                     if e['payload'].get(sx.STAMP) == grant.get('stamp')
                     and e['payload'].get('ask_sha256') == source.digest(evidence['snapshot']['ask'])]
        if len(old_offer) != 1 or not sx._bound(grant.get('stamp')):
            raise sx.Refused('local_grant_offer_invalid')
        prior = _prior_unblock(conn, evidence, old_offer[0])
        uid = _uid(conn, prior) if recovered else None
        if (events[0]['id'] < prior['id'] or (not recovered and events[0]['id'] != prior['id'])
                or grant != _grant(evidence, grant['stamp'], uid)):
            raise sx.Refused('local_grant_evidence_changed')
        return grant


def deliver(operation_id):
    """Plugin call point v1: authenticate canonical source, then core owns transition."""
    from hermes_cli.kanban_card_answer_runtime import _board_db
    from hermes_cli.kanban_card_answer_transport import Records
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing(_board_db('default')) as conn:
        sx._home(conn)
        sx.original(Records())
        with source.custody(operation_id) as evidence:
            return _deliver(conn, evidence)


def _deliver(conn, evidence):
    from hermes_cli.kanban_card_answer import _AlreadyRecorded
    from hermes_cli.kanban_reason_comment import FencedReasonComment
    stamp = evidence['snapshot']['ask'].get(sx.STAMP)
    if not sx._bound(stamp):
        raise sx.Refused('local_stamp_invalid')
    old = recorded(conn, stamp['offer_uid'])
    if old is not None:
        if old['operationId'] != evidence['operation']['operationId']:
            raise sx.Refused('local_operation_conflict')
        return evidence['receipt']
    if evidence['operation']['dispatchState'] == 'delivered':
        raise sx.Refused('local_delivered_requires_recovery')
    stamp = eligible(conn, evidence)
    grant = _grant(evidence, stamp, None)

    def before_insert(c):
        existing = recorded(c, stamp['offer_uid'])
        if existing is not None:
            if existing != grant:
                raise sx.Refused('local_operation_conflict')
            raise _AlreadyRecorded(evidence['receipt'])
        eligible(c, evidence)
        if sx._grant(c, stamp['offer_uid']) is not None:
            raise sx.Refused('local_offer_already_granted')
        row = c.execute('SELECT status,human_request FROM tasks WHERE id=?', (sx.TARGET.owner_task,)).fetchone()
        generation = c.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind IN "
                               "('created','blocked','block_loop_detected','unblocked','archived','completed')",
                               (sx.TARGET.owner_task,)).fetchone()[0]
        if (row['status'] not in ('blocked', 'triage') or json.loads(row['human_request'] or 'null') != evidence['snapshot']['ask']
                or generation != evidence['snapshot']['generation']):
            raise sx.Refused('local_ask_changed')

    def fence(c):
        before_insert(c)

    body = ('Choice recorded in Desktop: ' + source.encoded(dict(label='Unblock', value='unblock'))
            + '\nOperation: ' + evidence['operation']['operationId'])
    comment = FencedReasonComment('user-steps', body, before_insert)
    try:
        changed = kb.unblock_task(conn, sx.TARGET.owner_task, precondition=fence, comment=comment,
                                  event_extra={'user_steps_projection': evidence['receipt'], sx.GRANT: grant})
    except _AlreadyRecorded as done:
        return done.receipt
    if not changed:
        raise sx.Refused('local_transition_refused')
    return evidence['receipt']


def validate(conn, grant, *, now):
    with source.custody(grant['operationId'], delivered=True) as evidence:
        if grant != recorded(conn, grant['offer_uid']):
            raise sx.Refused('local_grant_changed')
        eligible(conn, evidence, now=now)


def recover(conn, records, operation_id):
    """Append authority for exactly the delivered historical operation; no lifecycle mutation."""
    if operation_id != RECOVERY_OPERATION:
        raise sx.Refused('local_recovery_operation_mismatch')
    sx._home(conn)
    sx.original(records)
    with source.custody(operation_id, delivered=True) as evidence, kb.write_txn(conn):
        stamp = evidence['snapshot']['ask'].get(sx.STAMP)
        if not sx._bound(stamp):
            raise sx.Refused('local_recovery_stamp_invalid')
        previous = recorded(conn, stamp['offer_uid'])
        if previous is not None:
            # Immutable replay can return even after consumption; it grants nothing new.
            if previous['operationId'] != operation_id:
                raise sx.Refused('local_grant_operation_conflict')
            return previous
        stamp = eligible(conn, evidence)
        if sx._grant(conn, stamp['offer_uid']) is not None:
            raise sx.Refused('local_offer_already_granted')
        prior = _prior_unblock(conn, evidence)
        grant = _grant(evidence, stamp, _uid(conn, prior))
        kb._append_event(conn, sx.TARGET.owner_task, RECOVERED, {sx.GRANT: grant})
        return grant
