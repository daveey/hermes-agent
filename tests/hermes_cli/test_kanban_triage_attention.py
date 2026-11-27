"""Triage age is attention, never human authority or execution permission."""
import math

import pytest

from hermes_cli import kanban_triage_attention as attention

NOW = 1_800_000_000


def event(kind, at, **payload):
    return {'kind': kind, 'created_at': at, 'payload': payload}


@pytest.mark.parametrize('age,overdue', [(599, False), (600, False), (601, True), (6 * 86400, True)])
def test_strict_boundary_and_six_days(age, overdue):
    row = {'status': 'triage', 'created_at': 1, 'events': [event('decision_retained', NOW - age)]}
    assert attention.current_entry(row) == NOW - age
    assert attention.overdue(row, now=NOW) is overdue
    assert 'human_request' not in row


def test_current_episode_survives_repeated_holds_and_clears_on_exit():
    events = [event('created', 1, status='ready'), event('contained', NOW - 1000),
              event('decision_retained', NOW - 10), event('heartbeat', NOW - 1)]
    row = {'status': 'triage', 'events': events}
    assert attention.current_entry(row) == NOW - 1000
    events.append(event('unblocked', NOW - 5))
    row['status'] = 'ready'
    assert attention.current_entry(row) is None
    events.append(event('decision_retained', NOW - 2))
    row['status'] = 'triage'
    assert attention.current_entry(row) == NOW - 2
    assert not attention.overdue(row, now=NOW)


@pytest.mark.parametrize('value', [None, True, '100', 0, -1, math.nan, math.inf, NOW + 1])
def test_invalid_or_future_time_is_unknown(value):
    row = {'status': 'triage', 'created_at': 1, 'events': [event('contained', value)]}
    assert not attention.overdue(row, now=NOW)
    assert attention.next_transition(row, now=NOW) is None


def test_creation_only_counts_if_it_entered_triage():
    assert attention.current_entry({'status': 'triage', 'created_at': 1}) is None
    assert attention.current_entry({'status': 'triage', 'events': [event('created', 1, status='ready')]}) is None
    assert attention.current_entry({'status': 'triage', 'events': [event('created', 1, status='triage')]}) == 1


def test_unchanged_input_crossing_and_recovery():
    row = {'status': 'triage', 'events': [event('contained', NOW)]}
    assert attention.next_transition(row, now=NOW + 599) > NOW + 600
    assert not attention.overdue(row, now=NOW + 600)
    assert attention.overdue(row, now=NOW + 601)
    row['status'] = 'done'
    assert not attention.overdue(row, now=NOW + 601)


@pytest.mark.parametrize('flag', ['stale', 'partial_source_unknown'])
def test_unreadable_rows_cannot_assert_attention(flag):
    row = {'status': 'triage', flag: True, 'events': [event('contained', 1)]}
    assert not attention.overdue(row, now=NOW)


@pytest.mark.parametrize('cause', [kind for kind, state in attention._FIXED.items() if state == 'triage'])
def test_every_triage_cause_gets_the_same_visual_policy(cause):
    assert attention.overdue({'status': 'triage', 'events': [event(cause, NOW - 601)]}, now=NOW)


@pytest.mark.parametrize('exit_kind', ['specified', 'decomposed', 'scheduled', 'review_requested'])
def test_administrative_exit_resets_episode(exit_kind):
    row = {'status': 'triage', 'events': [event('contained', NOW - 1000), event(exit_kind, NOW - 5), event('decision_retained', NOW - 1)]}
    assert attention.current_entry(row) == NOW - 1
    assert not attention.overdue(row, now=NOW)


def test_detached_markers_dedupe_aliases_and_do_not_double_count_human_asks():
    key = 'owner/profile/board/t_same'
    marker = attention.marker({'id': 't_same', 'key': key, 'status': 'triage', 'triage_entered_at': NOW - 601})
    row = {'needs_you': []}
    markers = marker | {(key, 'active', True), (key, 'stalled', False)}
    attention.project(row, markers | marker, now=NOW)
    assert row['kanban_triage_attention'] == {'count': 1, 'refs': [key], 'parked': 1, 'held': 0, 'stalled': 1}
    attention.project(row, markers | {(key, 'blocked', False)}, now=NOW)
    assert 'kanban_triage_attention' not in row
    assert row['needs_you'] == []
