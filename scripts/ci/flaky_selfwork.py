"""CI-flake producer opt-in and immutable manifest receipt delivery."""
from __future__ import annotations

import hashlib
import json

CORE_REPOSITORIES = frozenset({'softmax-universe/daveey-hermes', 'daveey/hermes'})
DECLARATION = {'version': 1, 'repository': 'hermes-core', 'area': 'ci', 'kind': 'ci-flake'}


def handle(journal, board, repo, page):
    """None means legacy BEFORE submission; pending declared work raises instead."""
    frozen = page['producer_request']
    operation = getattr(board, 'producer', None)
    if frozen is None:
        if page['attempted'] or repo not in CORE_REPOSITORIES or operation is None or not operation('capabilities'):
            return None
        digest = hashlib.sha256(page['payload'].encode()).hexdigest()
        create = {'title': f"CI flaky intake {repo} {page['day']}",
                  'body': (f"Daily CI flake intake for {repo}, UTC {page['day']}.\n"
                           "Preserve existing exact-test owners. Triage this immutable metadata manifest; "
                           "record each test's disposition and follow-up owner. No retry threshold changes. "
                           "Backlog/export: docs/ci-flaky-intake.md.\n\n" + page['payload']),
                  'idempotency_key': f"ci-flaky-intake:{repo}:{page['day']}:{digest}",
                  'assignee': board.assignee, 'self_work': DECLARATION}
        snapshot = operation('prepare', create)
        # Validate preparation before committing; it has not performed a submission.
        from hermes_cli.kanban_selfwork_producer import validate
        validate(snapshot)
        frozen = json.dumps(snapshot, sort_keys=True)
        with journal.db:
            journal.db.execute('UPDATE pages SET producer_request=?,attempted=1 WHERE id=?', (frozen, page['id']))
    if operation is None:
        raise RuntimeError('declared producer unavailable; never downgrade pending work')
    snapshot = json.loads(frozen)
    result = operation('advance', snapshot)
    if (not isinstance(result, dict) or type(result.get('version')) is not int or result['version'] != 1
            or result.get('status') != 'completed' or result.get('request_id') != snapshot['request_id']
            or result.get('disposition') not in ('create', 'append')):
        raise RuntimeError('declared producer receipt pending')
    owner = result.get('owner')
    if (not isinstance(owner, dict) or set(owner) != {'gateway', 'profile', 'board', 'task_id'}
            or {k: owner[k] for k in snapshot['home']} != snapshot['home']
            or not isinstance(owner['task_id'], str) or not owner['task_id']):
        raise RuntimeError('declared producer owner unavailable')
    ref = '/'.join(owner[k] for k in ('gateway', 'profile', 'board', 'task_id'))
    journal.acknowledge_page(page['id'], ref)
    return ref
