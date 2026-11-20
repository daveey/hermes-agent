"""Assignment repairs read foreign originals from the authenticated Board, never replicas."""
import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_assignment_routing as routing
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_assignment_routing as local_tests

board = local_tests.board
payload = local_tests.payload
task_with_drift = local_tests.task_with_drift


def _blocked(evidence):
    return next(row for row in evidence['events'] if row['kind'] == 'blocked')


def _rewrite_block_supervisor(evidence, other):
    event = _blocked(evidence)
    data = json.loads(event['payload'])
    data['supervisor_task_id'] = other
    event['payload'] = json.dumps(data)


def _duplicate_event(evidence, **updates):
    event = copy.deepcopy(_blocked(evidence))
    event.update(updates)
    evidence['events'].append(event)


@pytest.fixture
def foreign(board, monkeypatch):
    conn, home = board
    original = task_with_drift(board)
    assert kb.block_task(conn, original, kind='assignment', reason='Wrong worker')
    repair = payload(conn, original)['supervisor_task_id']
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (original,)).fetchone())
    task.update(uid=original, deleted_at=None)
    events = [dict(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=?', (original,))]
    for index, event in enumerate(events):
        event.update(uid=f'event-{index}', ord=index, deleted_at=None)
    evidence = dict(version=1, board='default', task_id=original, task=task,
                    runs=[], events=events, runs_complete=True, events_complete=True)
    conn.execute('DELETE FROM task_links WHERE child_id=?', (original,))
    conn.execute('DELETE FROM task_events WHERE task_id=?', (original,))
    conn.execute('DELETE FROM tasks WHERE id=?', (original,))
    conn.commit()
    calls = []
    monkeypatch.setattr(sync, 'sync_config', lambda: {'enabled': True})
    monkeypatch.setattr(sync, 'board_selected', lambda *args: True)
    monkeypatch.setattr(sync, 'board_slug_for_path', lambda *args: 'default')
    monkeypatch.setattr(client, 'load_identity', lambda: SimpleNamespace())

    def read(self, method, path, *args, **kwargs):
        assert not conn.in_transaction, 'network must precede the completion writer lock'
        assert method == 'GET'
        assert path == f'/v2/boards/default/tasks/{original}/execution-evidence'
        calls.append(path)
        return copy.deepcopy(evidence)

    monkeypatch.setattr(client.Hub, '_call', read)
    return conn, home, repair, original, evidence, calls


def test_foreign_original_completes_normally_without_materializing_it(foreign):
    conn, _, repair, original, _, calls = foreign
    assert kb.complete_task(conn, repair, result='Verified assignment')
    assert kb.get_task(conn, repair).status == 'done'
    assert kb.get_task(conn, original) is None
    assert len(calls) == 1


@pytest.mark.parametrize('mutate', [
    lambda e: e.update(task=None),
    lambda e: e['task'].update(assignee=None),
    lambda e: e['task'].update(deleted_at=1),
    lambda e: e['task'].update(id='t_0000000000000000'),
    lambda e: e.update(events_complete=False),
    lambda e: e['task'].pop('requires'),
    lambda e: e['task'].update(requires='{'),
    lambda e: e.update(events=[]),
    lambda e: next(row for row in e['events'] if row['kind'] == 'blocked').update(payload='{'),
    lambda e: e['task'].update(workspace_kind='dir', workspace_path=None),
    lambda e: e['task'].update(uid='t_0000000000000000'),
    lambda e: e.update(runs_complete=False),
    lambda e: e.update(version=2),
    lambda e: e.update(board='other'),
    lambda e: next(row for row in e['events'] if row['kind'] == 'blocked').update(task_id='t_0000000000000000'),
    lambda e: next(row for row in e['events'] if row['kind'] == 'blocked').update(deleted_at=1),
    lambda e: _rewrite_block_supervisor(e, 't_0000000000000000'),
    lambda e: e.update(task_id='t_0000000000000000'),
    lambda e: _blocked(e).update(payload='[]'),
    lambda e: _blocked(e).update(payload=json.dumps({
        **json.loads(_blocked(e)['payload']), 'reason': None})),
    lambda e: _duplicate_event(e, ord=max(row['ord'] for row in e['events']) + 1),
    lambda e: _blocked(e).update(ord='fixture-order'),
    lambda e: e['task'].update(model_override=42),
    lambda e: e['task'].update(skills=['']),
    lambda e: e.update(runs={}),
    lambda e: next(row for row in e['events'] if row['kind'] != 'blocked').update(kind=42),
    lambda e: _duplicate_event(e, uid='event-duplicate-order'),
], ids=['missing', 'unassigned', 'deleted', 'wrong-task', 'incomplete', 'missing-field',
        'bad-requires', 'missing-block', 'bad-block', 'missing-directory-path',
        'wrong-uid', 'runs-incomplete', 'wrong-version', 'wrong-board',
        'wrong-event-task', 'deleted-event', 'foreign-repair-block',
        'wrong-echo-task-id', 'non-dict-block', 'non-string-reason', 'duplicate-event-uid',
        'non-int-order', 'non-string-override', 'invalid-list-element', 'non-list-runs',
        'non-string-event-kind', 'duplicate-event-order'])
def test_foreign_evidence_fails_closed(foreign, mutate):
    conn, _, repair, _, evidence, _ = foreign
    mutate(evidence)
    with pytest.raises(routing.AssignmentRoutingError):
        kb.complete_task(conn, repair, result='Verified')
    assert kb.get_task(conn, repair).status != 'done'


@pytest.mark.parametrize('reason', ['unblock t_0000000000000000',
                                   'change model for t_0000000000000000'])
def test_foreign_original_authority_is_not_granted_by_profile(foreign, reason):
    conn, _, repair, _, evidence, _ = foreign
    event = next(e for e in evidence['events'] if e['kind'] == 'blocked')
    data = json.loads(event['payload'])
    data['reason'] = reason
    event['payload'] = json.dumps(data)
    with pytest.raises(routing.ExecutionAuthorityMismatch):
        kb.complete_task(conn, repair, result='Verified')
    assert kb.get_task(conn, repair).status != 'done'


def test_foreign_policy_drift_still_refuses(foreign):
    from hermes_cli.kanban_capabilities import CapabilityMismatchError
    conn, home, repair, _, _, _ = foreign
    (home / 'profiles/supervisor/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    with pytest.raises(CapabilityMismatchError):
        kb.complete_task(conn, repair, result='Verified')


def test_foreign_model_validation_is_not_skipped(foreign, monkeypatch):
    from hermes_cli import kanban_model_assignment as models
    conn, _, repair, _, evidence, _ = foreign
    evidence['task'].update(model_override='fixture-model', provider_override='fixture-provider')

    def refuse(assignee, model, provider):
        assert (assignee, model, provider) == ('supervisor', 'fixture-model', 'fixture-provider')
        raise ValueError('model refused')

    monkeypatch.setattr(models, 'validate_model_assignment', refuse)
    with pytest.raises(ValueError, match='model refused'):
        kb.complete_task(conn, repair, result='Verified')


def test_unenrolled_gateway_is_typed_refusal(foreign, monkeypatch):
    conn, _, repair, _, _, calls = foreign

    def unenrolled():
        raise client.SyncUnavailable('kanban sync: hub url, gateway name and key are all required')

    monkeypatch.setattr(client, 'load_identity', unenrolled)
    with pytest.raises(routing.AssignmentRoutingError):
        kb.complete_task(conn, repair, result='Verified')
    assert not calls
    assert kb.get_task(conn, repair).status != 'done'


@pytest.mark.parametrize('gate', ['disabled', 'unselected'])
def test_prepare_refuses_with_zero_board_reads_when_sync_not_selected(foreign, monkeypatch, gate):
    conn, _, repair, _, _, calls = foreign
    if gate == 'disabled':
        monkeypatch.setattr(sync, 'sync_config', lambda: {'enabled': False})
    else:
        monkeypatch.setattr(sync, 'board_selected', lambda *args: False)
    with pytest.raises(routing.AssignmentRoutingError):
        kb.complete_task(conn, repair, result='Verified')
    assert not calls
    assert kb.get_task(conn, repair).status != 'done'


def test_newest_matching_block_is_authority(foreign):
    conn, _, repair, _, evidence, _ = foreign
    matching = next(row for row in evidence['events'] if row['kind'] == 'blocked')
    older = copy.deepcopy(matching)
    data = json.loads(older['payload'])
    data['reason'] = 'unblock t_0000000000000000'
    older['payload'] = json.dumps(data)
    newer = copy.deepcopy(matching)
    newer.update(uid='event-newer', ord=max(row['ord'] for row in evidence['events']) + 1)
    evidence['events'].remove(matching)
    evidence['events'].extend([older, newer])
    assert kb.complete_task(conn, repair, result='Verified')


def test_board_failure_has_no_replica_fallback(foreign, monkeypatch):
    conn, _, repair, _, _, _ = foreign

    def unavailable(*args, **kwargs):
        raise OSError('unavailable')

    monkeypatch.setattr(client.Hub, '_call', unavailable)
    with pytest.raises(routing.AssignmentRoutingError):
        kb.complete_task(conn, repair, result='Verified')


def test_local_original_never_needs_board(board, monkeypatch):
    conn, _ = board
    original = task_with_drift(board)
    assert kb.block_task(conn, original, kind='assignment', reason='Wrong worker')
    repair = payload(conn, original)['supervisor_task_id']

    def forbidden(*args, **kwargs):
        pytest.fail('local assignment must not need network')

    monkeypatch.setattr(client, 'load_identity', forbidden)
    assert kb.complete_task(conn, repair, result='Verified')


@pytest.mark.parametrize('tamper', ['expired', 'key-changed', 'key-removed'])
def test_preflight_cannot_be_reused_for_another_key_or_after_deadline(foreign, monkeypatch, tamper):
    from hermes_cli import kanban_assignment_original as originals
    conn, _, repair, _, _, _ = foreign
    observation = originals.prepare(conn, repair)
    assert observation is not None
    if tamper == 'expired':
        monkeypatch.setattr(originals.time, 'monotonic', lambda: observation.expires)
    else:
        key = 'assignment-repair:t_0000000000000000' if tamper == 'key-changed' else None
        conn.execute('UPDATE tasks SET idempotency_key=? WHERE id=?', (key, repair))
        conn.commit()
    with kb.write_txn(conn), pytest.raises(routing.AssignmentRoutingError):
        routing.validate_repair_completion(conn, repair, observation=observation)


def test_no_network_if_caller_already_holds_transaction(foreign):
    from hermes_cli import kanban_assignment_original as originals
    conn, _, repair, _, _, calls = foreign
    with kb.write_txn(conn), pytest.raises(routing.AssignmentRoutingError):
        originals.prepare(conn, repair)
    assert not calls


def test_board_http_refusal_is_typed(foreign, monkeypatch):
    conn, _, repair, _, _, _ = foreign

    def unavailable(*args, **kwargs):
        raise client.HubError(403, 'fixture denied')

    monkeypatch.setattr(client.Hub, '_call', unavailable)
    with pytest.raises(routing.AssignmentRoutingError):
        kb.complete_task(conn, repair, result='Verified')


def test_scratch_wire_projection_needs_no_host_local_path(foreign):
    conn, _, repair, _, evidence, _ = foreign
    del evidence['task']['workspace_path']
    assert kb.complete_task(conn, repair, result='Verified')


@pytest.mark.parametrize('expired', [False, True])
def test_local_materialization_cannot_bypass_canonical_observation(foreign, monkeypatch, expired):
    from hermes_cli import kanban_assignment_original as originals
    conn, _, repair, original, evidence, _ = foreign
    observation = originals.prepare(conn, repair)
    assert observation is not None
    # Mimic task-before-history pickup while the preflight waits outside the writer lock.
    columns = [r[1] for r in conn.execute('PRAGMA table_info(tasks)') if r[1] in evidence['task']]
    conn.execute('INSERT INTO tasks (' + ','.join(columns) + ') VALUES ('
                 + ','.join('?' for _ in columns) + ')', [evidence['task'][c] for c in columns])
    conn.commit()
    assert kb.get_task(conn, original) is not None
    assert not conn.execute('SELECT 1 FROM task_events WHERE task_id=?', (original,)).fetchone()
    if expired:
        monkeypatch.setattr(originals.time, 'monotonic', lambda: observation.expires)
    with kb.write_txn(conn), pytest.raises(routing.AssignmentRoutingError, match='custody changed'):
        routing.validate_repair_completion(conn, repair, observation=observation)
    assert kb.get_task(conn, repair).status != 'done'

