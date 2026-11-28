"""Private diagnostic provenance survives a legacy promotion without a release receipt."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_sources import _route_owner
from hermes_cli.kanban_actionability_sources import describe
from tests.hermes_cli import test_kanban_release_stacked_capability as stacked

board = stacked.board


def test_promoted_capability_names_original_source_owner_and_release(board, monkeypatch):
    tid, old, current = stacked.stacked(board, monkeypatch)
    route = '@session:default@example/recorded-owner'
    for episode in (old, current):
        kb._append_event(board, tid, 'spend_budget_decision_ready', {
            'episode': episode, 'next_action': {'owner': {'kind': 'chat', 'refs': [route, route]}}})
    board.commit()
    before = stacked.helpers.written(board, tid)
    row = describe(board, tid)
    assert row['owner'] == {'kind': 'chat', 'refs': [route]}
    assert row['evidence_state'] == 'unknown'  # recorded routing is not chat liveness
    restrictions = row['private_restrictions']
    historical = next(r for r in restrictions if r['source_uid'] == old)
    assert historical['cause'] == 'capability_unavailable'
    assert historical['reason'] == 'restriction_unattributable'
    assert historical['release']['capability_episode'] == old
    assert historical['owner'] == {'kind': 'chat', 'refs': [route]}
    assert '--capability-episode' in historical['release']['condition']
    assert current in {r['source_uid'] for r in restrictions}
    reported = next(r for r in report(board, limit=100)['rows'] if r['task_id'] == tid)
    assert reported['private_restrictions'] == restrictions
    # This is read-only provenance, not permission to release either stop.
    assert stacked.helpers.status(board, tid) == 'triage'
    assert stacked.receipts(board, tid) == []
    assert stacked.helpers.written(board, tid) == before


def test_private_reason_default_preserves_classifier_return_type(board, monkeypatch):
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason

    tid, old, _ = stacked.stacked(board, monkeypatch)
    reader = Reader(board)
    task = reader.task(tid)
    assert _private_reason(board, task, reader) == 'card_in_triage'
    details = _private_reason(board, task, reader, explain=True)
    assert old in {row['source_uid'] for row in details}


def test_exact_guarded_release_removes_resolved_private_provenance(board, monkeypatch):
    tid, old, _ = stacked.stacked(board, monkeypatch)
    assert stacked.helpers.run(board, tid, capability_episodes=[old])['outcome'] == 'released'
    assert describe(board, tid)['private_restrictions'] == []


@pytest.mark.parametrize('refs,expected', [(None, []), ('not-a-list', []),
    (['', ' ', 1, None, 'recorded', 'recorded'], ['recorded'])])
def test_recorded_chat_routes_are_not_liveness_or_authority(refs, expected):
    assert _route_owner(None, {'kind': 'chats', 'refs': refs}) == {'kind': 'chat', 'refs': expected}


@pytest.mark.parametrize('kind,payload', [
    ('spend_budget_triaged', {'cause': 'safety_refusal'}),
    ('spend_budget_triaged', {'cause': 'capability_unavailable', 'superseded_by_safety_stop': True}),
    ('decision_hold_restricted', {'cause': 'capability_unavailable'}),
    ('spend_budget_triaged', {'cause': 'unknown'})])
def test_safety_and_unknown_sources_never_suggest_guarded_release(board, monkeypatch, kind, payload):
    tid, _, _ = stacked.stacked(board, monkeypatch)
    kb._append_event(board, tid, kind, payload)
    board.commit()
    row = describe(board, tid)
    newest = row['private_restrictions'][-1]
    assert newest['kind'] == kind and newest['cause'] == payload['cause']
    assert newest['release']['authority'] == 'unavailable'
    assert 'capability_episode' not in newest['release']


@pytest.mark.parametrize('synced', [False, True])
@pytest.mark.parametrize('home', [None, 'home-gateway'])
@pytest.mark.parametrize('kind,payload', [
    ('spend_budget_triaged', {}),
    ('spend_budget_triaged', {'cause': 'scope_stopped'}),
    ('spend_budget_triaged', {'cause': 'capability_unavailable'}),
    ('decision_hold_restricted', {'cause': 'capability_unavailable'}),
    ('spend_budget_triaged', 'invalid-json'),
])
def test_private_source_details_contract(synced, home, kind, payload):
    """Pure source-contract controls; no lifecycle writes or liveness claims."""
    from types import SimpleNamespace
    from hermes_cli.kanban_actionability_restrictions import details

    event = {'uid': 'stop', 'kind': kind, 'ord': 1, 'payload': payload}
    reader = SimpleNamespace(synced=synced, events=lambda _tid: [])
    row, = details({'id': 'fixture', 'created_on': home}, reader, [event], frozenset(), frozenset())
    assert row['source_uid'] == 'stop'
    assert row['home_gateway'] == home
    assert row['evidence_state'] == ('unknown' if synced else 'known')
    releasable = kind == 'spend_budget_triaged' and isinstance(payload, dict)
    assert row['release']['authority'] == ('existing_lifecycle_gates' if releasable else 'unavailable')
    if releasable:
        condition = row['release']['condition']
        assert (home or 'resolve home custody first') in condition
        capability = payload.get('cause') == 'capability_unavailable'
        assert ('--capability-episode stop' in condition) == capability
        assert ('capability_episode' in row['release']) == capability


@pytest.mark.parametrize('explain', [False, True])
@pytest.mark.parametrize('failure', ['incomplete', 'sql', 'empty', 'uidless'])
def test_private_reason_missing_evidence_has_typed_empty_result(explain, failure):
    import sqlite3
    from types import SimpleNamespace
    from hermes_cli.kanban_actionability_sources import _private_reason

    def rows(*_args):
        if failure == 'sql':
            raise sqlite3.OperationalError('fixture unavailable')
        return [{'kind': 'spend_budget_triaged'}] if failure == 'uidless' else []

    reader = SimpleNamespace(events_complete=failure != 'incomplete', event_columns=set(),
                             rows=rows, uid=lambda _row: None)
    assert _private_reason(None, {'id': 'fixture'}, reader, explain=explain) == ([] if explain else None)


@pytest.mark.parametrize('admin,expected', [(None, False),
    ({'kind': 'chat', 'refs': ['recorded']}, True),
    ({'kind': 'operator', 'refs': ['recorded']}, False)])
def test_recorded_chat_is_independent_unknown_fact(monkeypatch, admin, expected):
    """Reducer seam contract: isolate chat uncertainty from synced/partial/source flags."""
    from types import SimpleNamespace
    from hermes_cli import kanban_actionability_sources as sources
    from hermes_cli import kanban_actionability_confirmed as confirmed

    task = {'id': 'fixture', 'status': 'triage'}
    event = {'uid': 'stop', 'kind': 'spend_budget_triaged',
             'data': {'cause': 'scope_stopped'}, 'created_at': 1}
    reader = SimpleNamespace(synced=False, partial=False, task=lambda _tid: task,
        events=lambda _tid: [event], uid=lambda row: row['uid'],
        pending=lambda _tid: None, columns=lambda _table: set())
    monkeypatch.setattr(sources, '_state', lambda *_args: (event, False, []))
    monkeypatch.setattr(sources, '_dependencies', lambda *_args: ([], False))
    monkeypatch.setattr(sources, '_administration', lambda *_args: (admin, {'escalate_at': 100}))
    monkeypatch.setattr(sources, '_release_offer', lambda *_args: None)
    monkeypatch.setattr(sources, '_private_reason', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sources, 'project', lambda facts, **_kwargs: dict(facts))
    monkeypatch.setattr(confirmed, 'compose', lambda _reader, _task, _facts, row, **_kwargs: row)
    row = sources._describe(None, 'fixture', now=1, reader=reader)
    assert bool(row['unknown']) is expected
    assert row['administrative_owner'] == admin


@pytest.mark.parametrize('payload', [{}, 'not-json'])
def test_nonstop_context_never_becomes_restriction_source(payload):
    from types import SimpleNamespace
    from hermes_cli.kanban_actionability_restrictions import details

    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    events = [{'uid': 'renewal', 'kind': 'scope_renewed', 'ord': 1, 'payload': payload}]
    assert details({'id': 'fixture'}, reader, events, frozenset(), frozenset()) == []


def test_details_excludes_resolved_sources_and_nonstop_context():
    from types import SimpleNamespace
    from hermes_cli.kanban_actionability_restrictions import details

    events = [
        {'uid': 'budget', 'kind': 'spend_budget_triaged', 'ord': 1, 'payload': {}},
        {'uid': 'capability', 'kind': 'spend_budget_triaged', 'ord': 2,
         'payload': {'cause': 'capability_unavailable'}},
        {'uid': 'promoted', 'kind': 'promoted_manual', 'ord': 3, 'payload': {}},
        {'uid': 'renewal', 'kind': 'scope_renewed', 'ord': 4,
         'payload': {'covered_restriction_uids': ['resolved-scope']}},
    ]
    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    rows = details({'id': 'fixture'}, reader, events, frozenset({'resolved-scope'}), frozenset())
    assert [(row['source_uid'], row['reason']) for row in rows] == [
        ('capability', 'restriction_unattributable')]
