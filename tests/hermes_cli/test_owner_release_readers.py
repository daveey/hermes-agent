"""Real persisted reader composition; fixture application is not live HOME admission."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_scope_renewal as renewal, kanban_spend_decision as decision
from hermes_cli import kanban_release as release, kanban_release_application as applied
from tests.hermes_cli import test_owner_release_application as helpers

proof = helpers.proof
store = helpers.store


def prepare(conn, receipt):
    result = helpers.setup(conn, receipt)
    conn.execute('CREATE TABLE task_comments(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,body TEXT)')
    conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,created_on TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('t_target','ready','home')")
    # Preserve historical order: stops precede renewal, releases and promotion.
    for uid, cause in [('stop-1', 'scope_stopped'), ('restriction-1', 'scope_stopped'), ('older-1', 'capability_unavailable')]:
        kind = 'decision_hold_restricted' if uid == 'restriction-1' else 'spend_budget_triaged'
        helpers.add(conn, {**helpers.event_row(uid, kind, {'cause': cause, 'episode': 'stop-1'}), 'created_at': 1})
    conn.execute("UPDATE task_events SET id=-id WHERE uid IN ('stop-1','restriction-1','older-1')")
    return result


def test_consumed_and_public_effects_do_not_enable_any_reader(proof, store):
    conn, _ = store
    prepare(conn, proof)
    assert renewal.renewals(conn, 't_target') == []
    assert renewal.covered(conn, 't_target') == frozenset()
    assert decision.execution_restricted(conn, 't_target')
    assert release.current_mandate(conn, 't_target') is None
    assert release._release_event(conn, 't_target') is None
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    assert _private_reason(conn, dict(conn.execute('SELECT * FROM tasks').fetchone()), Reader(conn)) is not None


def test_applied_enables_exact_scope_context_and_keeps_accounting_floor(proof, store):
    conn, path = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    assert renewal.covered(conn, 't_target') == frozenset({'stop-1', 'restriction-1'})
    assert not decision.execution_restricted(conn, 't_target')
    assert decision.restricted_floor(conn, 't_target') == 1
    expected = json.loads(result['payload'])['mandate']
    mandate = renewal.mandate(conn, 't_target')
    assert mandate['body'] == expected
    assert mandate['mandate_kind'] == 'owner_release_intent'
    assert 'mandate_comment_id' not in mandate
    current = release.current_mandate(conn, 't_target')
    assert release._release_event(conn, 't_target') is not None
    assert current['body'] == expected
    assert current['intent_uid'] == 'intent-1'
    assert 'comment_id' not in current
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    task = dict(conn.execute('SELECT * FROM tasks').fetchone())
    assert _private_reason(conn, task, Reader(conn)) is None
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as ro:
        ro.row_factory = sqlite3.Row
        ro.execute('PRAGMA query_only=ON')
        assert renewal.covered(ro, 't_target') == frozenset({'stop-1', 'restriction-1'})
        assert release.current_mandate(ro, 't_target')['body'] == expected


@pytest.mark.parametrize('kind,cause', [('decision_hold_restricted','scope_stopped'), ('spend_budget_triaged','safety_refusal'), ('administrative_stop_restricted',None)])
def test_independent_restrictions_remain_after_applied(proof, store, kind, cause):
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    helpers.add(conn, {**helpers.event_row('new-stop', kind, {'cause': cause}), 'created_at': 102})
    assert decision.execution_restricted(conn, 't_target')
    assert renewal.mandate(conn, 't_target') is None
    assert release.current_mandate(conn, 't_target') is None
    assert decision.restricted_floor(conn, 't_target') == 1


def test_applied_context_rendering_never_invents_comment(proof, store, monkeypatch):
    from hermes_cli import kanban_db_context as context
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    monkeypatch.setattr(context._kb, 'list_comments', lambda *_: [])
    lines = []
    context._ctx_release(lines, conn, 't_target', now=105)
    context._ctx_comments(lines, conn, 't_target', now=105)
    text = '\n'.join(lines)
    assert text.count(json.loads(result['payload'])['mandate']) == 1
    assert 'intent intent-1' in text
    assert 'comment None' not in text
    assert '## Scope renewal mandate' not in text


def test_corrupt_application_revokes_all_readers(proof, store):
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    conn.execute("DELETE FROM task_events WHERE uid='budget-1'")
    assert renewal.covered(conn, 't_target') == frozenset()
    assert renewal.mandate(conn, 't_target') is None
    assert release.current_mandate(conn, 't_target') is None
    assert decision.execution_restricted(conn, 't_target')


def test_intent_marker_cannot_borrow_legacy_comment_validation(proof, store):
    conn, _ = store
    prepare(conn, proof)
    body = 'Real legacy comment, not authorization of this native intent'
    conn.execute("INSERT INTO task_comments VALUES (1,'comment-1','t_target',?)", (body,))
    p = json.loads(proof['renewal']['payload'])
    p.update(mandate_comment_uid='comment-1', mandate_body_sha256=renewal.digest(body))
    conn.execute("UPDATE task_events SET payload=? WHERE uid='consume-1'", (json.dumps(p),))
    assert renewal.covered(conn, 't_target') == frozenset()


def test_applied_release_event_lookup_requires_exact_frozen_effect(proof, store):
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    event = dict(conn.execute("SELECT * FROM task_events WHERE uid='release-current'").fetchone())
    assert applied.verified_release(conn, event) is not None
    event['uid'] = 'lookalike'
    assert applied.verified_release(conn, event) is None
    event['uid'] = 'release-current'
    event['task_id'] = 'another-task'
    assert applied.verified_release(conn, event) is None


@pytest.mark.parametrize('cause', [None, 'capability_unavailable'])
def test_successor_stop_cannot_inherit_native_mandate(proof, store, cause, monkeypatch):
    from hermes_cli import kanban_db_context as context
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    helpers.add(conn, {**helpers.event_row('successor', 'spend_budget_triaged', {'cause': cause}), 'created_at': 102})
    assert renewal.mandate(conn, 't_target') is None
    assert release.current_mandate(conn, 't_target') is None
    assert renewal.covered(conn, 't_target') == frozenset({'stop-1', 'restriction-1'})
    monkeypatch.setattr(context._kb, 'list_comments', lambda *_: [])
    lines = []
    context._ctx_release(lines, conn, 't_target', now=105)
    context._ctx_comments(lines, conn, 't_target', now=105)
    assert json.loads(result['payload'])['mandate'] not in '\n'.join(lines)


@pytest.mark.parametrize('value', [None, '', False, {}, 'missing'])
def test_native_sibling_markers_never_downgrade_to_legacy(proof, store, value):
    conn, _ = store
    prepare(conn, proof)
    body = 'Legacy positive control'
    conn.execute("INSERT INTO task_comments VALUES (1,'comment-1','t_target',?)", (body,))
    p = json.loads(proof['renewal']['payload'])
    p.update(mandate_comment_uid='comment-1', mandate_body_sha256=renewal.digest(body))
    p.pop('owner_release_intent_uid')
    if value != 'missing':
        p['owner_release_intent_uid'] = value
    conn.execute("UPDATE task_events SET payload=? WHERE uid='consume-1'", (json.dumps(p),))
    assert renewal.covered(conn, 't_target') == frozenset()
    # Stripping every public marker cannot erase retained private native identity.
    for key in ('owner_release_intent_uid','owner_release_digest'):
        p.pop(key, None)
    conn.execute("UPDATE task_events SET payload=? WHERE uid='consume-1'", (json.dumps(p),))
    assert renewal.covered(conn, 't_target') == frozenset()
    # A genuinely distinct legacy UID, not a mutated privately-certified native
    # UID, still uses the preexisting comment authority branch.
    conn.execute("UPDATE task_events SET uid='legacy-renewal' WHERE uid='consume-1'")
    assert renewal.covered(conn, 't_target') == frozenset({'stop-1', 'restriction-1'})


@pytest.mark.parametrize('table', [
    'owner_release_consumption_receipts', 'owner_release_applied_receipts', 'owner_release_executor_receipts',
])
@pytest.mark.parametrize('shape', ['valid', 'missing-column'])
def test_private_native_identity_is_hold_only_even_with_damaged_receipt(table, shape):
    with sqlite3.connect(':memory:') as conn:
        event = {'uid': 'native-uid'}
        assert not applied.native_renewal(conn, event, {})
        conn.execute(f'CREATE TABLE {table} ({"consume_uid" if shape == "valid" else "unrelated"} TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?)', ('native-uid',))
        assert applied.native_renewal(conn, event, {})
        if shape == 'valid':
            assert not applied.native_renewal(conn, {'uid': 'legacy-uid'}, {})
        # Public native classification remains held even on a pre-native schema.
        assert applied.native_renewal(conn, {'uid': 'other'}, {'owner_release_intent_uid': None})


@pytest.mark.parametrize('origin', ['home', 'other', None])
def test_public_capability_release_needs_applied_even_at_home(proof, store, origin):
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    conn, _ = store
    prepare(conn, proof)
    conn.execute("DELETE FROM task_events WHERE uid IN ('stop-1','restriction-1','consume-1')")
    conn.execute("UPDATE task_events SET origin_gateway=? WHERE kind='stop_released'", (origin,))
    task = dict(conn.execute('SELECT * FROM tasks').fetchone())
    assert _private_reason(conn, task, Reader(conn)) == 'restriction_unattributable'


@pytest.mark.parametrize('custody', ['home', 'executor'])
def test_erased_native_release_keeps_retained_private_identity(proof, store, custody):
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    if custody == 'executor':
        # Defensive damaged-import fixture: retain the same frozen effect UIDs in
        # the executor envelope, not an authenticated foreign certification proof.
        evidence = json.loads(conn.execute(f'SELECT evidence FROM {applied.TABLE}').fetchone()[0])
        conn.execute('CREATE TABLE owner_release_executor_receipts(task_id TEXT,evidence TEXT)')
        conn.execute('INSERT INTO owner_release_executor_receipts VALUES (?,?)',
                     ('t_target', json.dumps({'events': evidence['effects']})))
        conn.execute(f'DROP TABLE {applied.TABLE}')
    # Deliberately damaged imported history, not ordinary Board mutable ingress.
    # Keep the immutable private effect identities, but erase every public native
    # marker and supply coherent legacy mandate bindings at HOME.
    body = 'A real legacy comment cannot authorize an erased native release'
    conn.execute("INSERT INTO task_comments VALUES (1,'comment-1','t_target',?)", (body,))
    for event in conn.execute("SELECT uid,payload FROM task_events WHERE kind='stop_released'").fetchall():
        payload = json.loads(event['payload'])
        payload = {k: v for k, v in payload.items() if not k.startswith('owner_release_') and k != 'consume_uid'}
        payload['mandate_comment_uid'] = 'comment-1'
        conn.execute('UPDATE task_events SET payload=? WHERE uid=?', (json.dumps(payload), event['uid']))
    # Isolate the old capability rather than masking it with a still-held scope.
    conn.execute("DELETE FROM task_events WHERE uid IN ('stop-1','restriction-1','consume-1')")
    assert applied.verified_application(conn, 'intent-1') is None
    assert release._release_event(conn, 't_target') is None
    assert release.current_mandate(conn, 't_target') is None
    task = dict(conn.execute('SELECT * FROM tasks').fetchone())
    assert _private_reason(conn, task, Reader(conn)) == 'restriction_unattributable'
    # Distinct genuine legacy release UIDs remain compatible despite unrelated
    # retained private native custody for the same task.
    conn.execute("UPDATE task_events SET uid='legacy-'||uid WHERE kind='stop_released'")
    assert release._release_event(conn, 't_target') is not None
    assert release.current_mandate(conn, 't_target')['body'] == body
    assert _private_reason(conn, task, Reader(conn)) is None


def test_applied_never_authorizes_a_later_triage_repair(proof, store, monkeypatch):
    from hermes_cli import kanban_decided_triage as maintenance
    from hermes_cli import kanban_containment_guard, kanban_goal_recovery, kanban_resume_requester, kanban_wake_check
    conn, _ = store
    result = prepare(conn, proof)
    helpers.record(conn, result)
    for column in ('human_request', 'block_kind', 'claim_lock', 'current_run_id'):
        conn.execute(f'ALTER TABLE tasks ADD COLUMN {column} TEXT')
    conn.execute("UPDATE tasks SET status='triage'")
    # Keep all unrelated maintenance refusals explicit; the real renewal selection
    # and APPLIED exclusion must be the reason no new promotion is written.
    monkeypatch.setattr(kanban_containment_guard, 'pending', lambda *_: None)
    monkeypatch.setattr(kanban_goal_recovery, 'current_park', lambda *_: None)
    monkeypatch.setattr(kanban_resume_requester, 'open_recovery_card', lambda *_: None)
    monkeypatch.setattr(kanban_wake_check, 'foreign_copy', lambda *_: False)
    stop = dict(conn.execute("SELECT * FROM task_events WHERE uid='stop-1'").fetchone())
    monkeypatch.setattr(decision, 'strict_episode', lambda *_: stop)
    from hermes_cli import kanban_release_comments

    def no_borrowed_read_permission(*args, **kwargs):
        raise AssertionError('APPLIED must not be reconsidered as maintenance authority')

    monkeypatch.setattr(kanban_release_comments, 'check', no_borrowed_read_permission)
    assert maintenance._recorded(conn, 't_target') is None
    before = [tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')]
    assert maintenance.repair_decided_triage(conn) == []
    assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'triage'
    assert [tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')] == before
