"""Read a recorded user-steps operation without creating/migrating its store.

Only the owner-bound courier calls this private adapter. A claimed sourceRef or
caller-supplied recorded flag is never consulted. Source and path come from the
receiving gateway's profile configuration, not from the request body.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_cli.kanban_card_answer import AnswerRefused


def recorded(path: Path, **kwargs) -> dict:
    """Controlled boundary: a malformed/absent source store or row is a refusal.

    Only genuinely transient SQLite lock/busy conditions stay retryable (OSError);
    anything else a stored operation can do wrong becomes ``AnswerRefused``.
    """
    try:
        return _recorded(path, **kwargs)
    except AnswerRefused:
        raise
    except sqlite3.OperationalError as exc:
        if any(word in str(exc).lower() for word in ('locked', 'busy')):
            raise OSError('source_store_busy') from None
        raise AnswerRefused('source_store_unavailable') from None
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError, AttributeError, IndexError, UnicodeError):
        raise AnswerRefused('source_operation_malformed') from None


def _recorded(path: Path, *, source: dict, owner_gateway: str, request_id: str, answer: dict) -> dict:
    """Exact namespace/session operation + producer-owned remote binding.

    The binding's snapshot.remote contains version=1, target (the six owner/ask
    coordinates), and choiceValues (request choice ID -> exact owner choice).
    The ordinary projection row and immutable frozen request still apply.
    """
    ns = json.dumps([source['gateway'], source['profile']], separators=(',', ':'), ensure_ascii=False)
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        row = conn.execute('SELECT body FROM operations WHERE namespace=? AND session=? AND id=?',
                           (ns, source['session'], answer['operationId'])).fetchone()
        if row is None:
            raise AnswerRefused('source_operation_missing')
        op = json.loads(row['body'])
        expected_owner = dict(gatewayId=source['gateway'], profile=source['profile'], storedSessionId=source['session'])
        if (op.get('owner') != expected_owner or op.get('operationId') != answer['operationId']
                or op.get('dispatchState') == 'cancelled' or not op.get('recordedAt')):
            raise AnswerRefused('source_operation_mismatch')
        bound = conn.execute('SELECT * FROM kanban_projections WHERE namespace=? AND session=? AND request_id=?',
                             (ns, source['session'], op['requestId'])).fetchone()
        intent = op['intent']
        if bound is None or json.loads(bound['request']) != intent['request']:
            raise AnswerRefused('source_binding_missing')
        frozen = json.loads(bound['request'])
        remote = json.loads(bound['snapshot']).get('remote')
        target = {k: v for k, v in answer.items() if k not in ('choice', 'operationId')}
        if (not isinstance(remote, dict) or type(remote.get('version')) is not int or remote['version'] != 1
                or remote.get('target') != target or target['ownerGateway'] != owner_gateway
                or bound['board'] != target['board'] or bound['task'] != target['task']):
            raise AnswerRefused('source_target_mismatch')
        decision = op['decision']
        if (op.get('revision') != frozen['revision'] or decision.get('expectedRevision') != frozen['revision']
                or decision.get('requestId') != frozen['requestId'] or op['requestId'] != frozen['requestId']):
            raise AnswerRefused('source_revision_mismatch')
        choices = [ch for ch in frozen['choices'] if ch['id'] == decision.get('choiceId')]
        values = remote.get('choiceValues')
        if (len(choices) != 1 or choices[0].get('effect') != 'answer'
                or not isinstance(values, dict) or values.get(decision.get('choiceId')) != answer['choice']
                or intent.get('effect') != 'answer' or intent.get('revisionText') != choices[0]['label']
                or intent.get('executionAuthorized') is not False or intent.get('permissionGrants') != []):
            raise AnswerRefused('source_choice_mismatch')
        return dict(answer=answer, source=source, requestId=request_id, gateway=source['gateway'], recorded=True)
    finally:
        conn.close()
