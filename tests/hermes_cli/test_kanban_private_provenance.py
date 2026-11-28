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
