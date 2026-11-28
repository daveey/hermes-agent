"""A reserved revised approval is application recovery, not another human ask.

This read-only ActionStore adapter recognizes custody, NEVER release authority.
The original producer's stamped projection binds the stop; a later explicit
approval revision may differ from it. Neither frozen revision is rewritten.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from hermes_cli.kanban_card_answer import digest

EVENT = 'release_application_pending'


def _revision(source, ns, request, revision):
    row = source.execute('SELECT body FROM revisions WHERE namespace=? AND id=? AND revision=?',
                         (ns, request, revision)).fetchone()
    return json.loads(row[0]) if row else None


def _operation(source, row, ns, profile, install, task, stop):
    op, snapshot, projected = (json.loads(row[k]) for k in ('body', 'snapshot', 'request'))
    owner = dict(gatewayId='install:' + install, profile=profile, storedSessionId=row['session'])
    oid, request, revision = op['operationId'], row['request_id'], op['revision']
    if (op.get('operationId') != row['oid'] or op.get('owner') != owner
            or op.get('requestId') != request or op.get('dispatchState') != 'delivered'
            or not op.get('recordedAt') or op.get('retired') or type(revision) is not int
            or snapshot.get('task') != task or snapshot.get('session') != row['session']
            or snapshot.get('ask', {}).get('release_target') != stop
            or projected.get('owner') != owner or projected.get('requestId') != request
            or type(projected.get('revision')) is not int or projected['revision'] >= revision
            or _revision(source, ns, request, projected['revision']) != projected):
        return None
    intent, decision = op['intent'], op['decision']
    frozen = intent['request']
    if (frozen != _revision(source, ns, request, revision) or frozen.get('owner') != owner
            or frozen.get('requestId') != request or frozen.get('revision') != revision
            or frozen.get('kind') != 'approval' or intent.get('effect') != 'approve'
            or intent.get('executionAuthorized') is not True
            or decision.get('requestId') != request or decision.get('expectedRevision') != revision
            or decision.get('idempotencyKey') != op.get('idempotencyKey')):
        return None
    chosen = [c for c in frozen['choices'] if c.get('id') == decision.get('choiceId')]
    if len(chosen) != 1 or chosen[0].get('effect') != 'approve':
        return None
    current = source.execute('SELECT revision,state,resolution FROM requests WHERE namespace=? AND session=? AND id=?',
                             (ns, row['session'], request)).fetchone()
    resolution = dict(choiceId=decision['choiceId'], operationId=oid, revision=revision, recordedAt=op['recordedAt'])
    if (current is None or current[0] != revision or current[1] != 'resolved'
            or json.loads(current[2] or 'null') != resolution):
        return None
    scope = digest({k: v for k, v in frozen.items() if k not in ('state', 'resolution', 'createdAt', 'withdrawal')})
    if op.get('binding', {}).get('scopeDigest') != scope:
        return None
    receipts = source.execute('SELECT phase,fingerprint,body FROM owner_receipts WHERE namespace=? AND operation=?',
                              (ns, oid)).fetchall()
    if {r[0] for r in receipts} != {'acknowledged', 'reserved'} or len(receipts) != 2:
        return None
    key = digest([ns, oid, revision, scope])
    for phase, fingerprint, raw in receipts:
        receipt = json.loads(raw)
        body = dict(operationId=oid, requestId=request, revision=revision, scopeDigest=scope, phase=phase, evidence=[])
        if phase == 'reserved':
            body['applicationKey'] = key
        if (any(receipt.get(k) != v for k, v in body.items()) or fingerprint != digest(body)
                or receipt.get('fingerprint') != fingerprint or not receipt.get('recordedAt')):
            return None
    return dict(operation=oid, request=request, revision=revision, application_key=key,
                scope_digest=scope, stop=stop, projected_ask=snapshot['ask'])


def _read(conn, task, stop):
    from hermes_cli.kanban_card_answer_runtime import _profile
    from hermes_cli.install_identity import get_install_id
    from hermes_cli.kanban_release_offer import _board
    profile, home = _profile()
    path = home / 'user-steps/actions.sqlite3'
    if not path.is_file():
        return None
    install = get_install_id()
    if not install:
        return None
    ns = json.dumps(['install:' + install, profile], separators=(',', ':'), ensure_ascii=False)
    # ActionStore is not a Kanban board. No migration, row writes, or plugin import.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as source:  # noqa: kanban-lockguard
        source.row_factory = sqlite3.Row
        source.execute('BEGIN')
        if not source.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_projections' AND type='table'").fetchone():
            return None  # optional Mirror schema is not created by ActionStore itself
        if not source.execute('SELECT 1 FROM kanban_projections WHERE namespace=? AND board=? AND task=? LIMIT 1',
                              (ns, _board(conn), task)).fetchone():
            return None  # unrelated operations/store-schema errors cannot hide this card's backstop
        rows = source.execute('SELECT o.id AS oid,o.body,p.snapshot,p.request,p.session,p.request_id FROM kanban_projections p '
                              'JOIN operations o ON o.namespace=p.namespace AND o.session=p.session '
                              "AND json_extract(o.body,'$.requestId')=p.request_id "
                              'WHERE p.namespace=? AND p.board=? AND p.task=? LIMIT 65',
                              (ns, _board(conn), task)).fetchall()
        if len(rows) > 64:
            raise ValueError('reserved approval candidate bound exceeded')
        found = [_operation(source, row, ns, profile, install, task, stop) for row in rows]
        matches = [item for item in found if item is not None]
        return matches[0] if len(matches) == 1 else None


def _filed(conn, task, ask):
    from hermes_cli import kanban_release as release
    return any(e['payload'].get('release_target') == ask.get('release_target')
               and e['payload'].get('ask_sha256') == release.ask_digest(ask)
               for e in release.own_events(conn, task, (release.ESCALATED, release.REFILED)))


def _decision(value):
    result = {k: v for k, v in value.items() if k != 'evidence'}
    lines = str(value.get('scope', '')).splitlines()
    if sum(line.startswith('Why it stopped: ') for line in lines) == 1:
        result['scope'] = '\n'.join('Why it stopped: ' if line.startswith('Why it stopped: ') else line
                                    for line in lines)
    return result


def _legacy_migration(conn, row, old, current):
    """Recognize only the two complete HOME producer templates, not a stamp alone.

    Regenerate against the unchanged work summary. Historical reason/age and
    current reason/evidence are context; all decision text and costs must match.
    This is suppression only: the templates have different dismissal semantics.
    """
    import re
    from hermes_cli import kanban_release_offer as offers
    from hermes_cli import kanban_stranded_brief as brief
    from hermes_cli.kanban_stranded_park import park_entry
    summary, _, _ = brief.context(conn, row, park_entry(conn, row['id']))
    prefix = f'Situation: {summary} '
    scope = old.get('scope', '')
    age = re.fullmatch(r'Tried: checking for an active owner and a usable request after ([0-9]{1,8})h stopped\. '
                       r'Only you can: choose whether to dismiss this work or give its owner new direction\.',
                       old.get('why_human', ''))
    if not age or not scope.startswith(prefix) or '\nOptions:\n' not in scope:
        return False
    reason = scope[len(prefix):].split('\nOptions:\n', 1)[0]
    entry = dict(kind='spend_budget_triaged', payload=dict(summary=reason))
    expected = brief.request(conn, row, entry, int(age[1]) * 3600, old.get('evidence', ''))
    if old != expected:
        return False
    link = re.match(r'Card: @task:([^\s/@]+)@([^\s/]+)/([^\s.]+)\. What it is: ', current.get('scope', ''))
    if not link or link[1] != offers._board(conn) or link[3] != old['release_target']['task']:
        return False
    return any(_decision(current) == _decision(offers.request(conn, row, code, gateway=link[2]))
               for code in offers._WHY)


def reconcile(conn, task, *, for_filing=False):
    """Suppress a repeated offer; clear only an exact home-filed ask on this stop.

    Returns true for pending application custody, false for no proven match.
    Unreadable existing source raises: callers defer this candidate, not infer a
    new human decision. No release, budget, status or source-store writes occur.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release as release
    from hermes_cli.kanban_ask_resolved import store_request
    from hermes_cli.kanban_home_transfer import frozen
    from hermes_cli.kanban_stranded_park import PARKED, _leased
    import time
    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if row is None or row['status'] not in PARKED or frozen(conn, task) or _leased(dict(row), int(time.time())):
            return False
        stop = release.offer(conn, task)
        if stop is None:
            return False
        proof = _read(conn, task, stop)
        if proof is None or not _filed(conn, task, proof['projected_ask']):
            return False
        ask = json.loads(row['human_request'] or 'null')
        projected = proof['projected_ask']
        # A later HOME producer can refresh contextual scope/evidence without
        # changing the decision. Require its own complete filing digest too;
        # a matching stamp on an edited column alone has no custody.
        # Only the producer's single stop-reason line may change in scope.
        # Body/summary, cost and options remain decision content, not context.
        exact = ask == projected or (isinstance(ask, dict) and _filed(conn, task, ask)
                                     and (_decision(ask) == _decision(projected)
                                          or _legacy_migration(conn, dict(row), projected, ask)))
        if exact:
            store_request(conn, task, None)
        record = {k: v for k, v in proof.items() if k != 'projected_ask'}
        record['execution_authorized'] = False
        if not any(e['payload'] == record for e in release.own_events(conn, task, (EVENT,))):
            kb._append_event(conn, task, EVENT, record)
        # Other human requests retain normal backstop handling, but a generic
        # release filing must not replace them while application is unresolved.
        return ask is None or exact or for_filing
