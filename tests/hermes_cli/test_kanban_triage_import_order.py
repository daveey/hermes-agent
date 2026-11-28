"""Arrival order after rehoming is not the lifecycle clock (display only)."""
import copy
import json
import math
import sqlite3
import uuid

import pytest

from hermes_cli import kanban_triage_attention as attention

NOW = 1_800_000_000


def event(kind, at, *, uid=None, identity=0, **payload):
    return dict(kind=kind, created_at=at, payload=payload, uid=uid, id=identity)


def v7(at_ms):
    return str(uuid.UUID(int=(at_ms << 80) | (7 << 76) | (2 << 62)))


def entry(events, *, stored=False, uid_column=True):
    row = {'id': 't_imported', 'status': 'triage', 'events': events}
    if not stored:
        return attention.current_entry(row)
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, '
                     'payload TEXT, created_at REAL' + (', uid TEXT)' if uid_column else ')'))
        for item in events:
            values = ['t_imported', item['kind'], json.dumps(item['payload']), item['created_at']]
            if uid_column:
                values.append(item.get('uid'))
            conn.execute('INSERT INTO task_events (task_id,kind,payload,created_at' +
                         (',uid' if uid_column else '') + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
        return attention.annotate(conn, [row])[0]['triage_entered_at']


@pytest.mark.parametrize('stored,uid_column', [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize('age', [600, 601, 6 * 86400])
def test_rehomed_older_history_arrives_after_current_hold(stored, uid_column, age):
    at = NOW - age
    events = [event('spend_budget_triaged', at), event('decision_retained', at + 1),
              event('created', at - 200, status='ready'), event('claimed', at - 30),
              event('spawned', at - 29)]
    original = copy.deepcopy(events)
    entered = entry(events, stored=stored, uid_column=uid_column)
    assert entered == at
    assert attention.overdue({'status': 'triage', 'triage_entered_at': entered}, now=NOW) is (age > 600)
    assert events == original


@pytest.mark.parametrize('stored', [False, True])
def test_portable_milliseconds_beat_remapped_same_second_ids(stored):
    at = NOW - 601
    events = [event('contained', at, uid=v7(at * 1000 + 800), identity=1),
              event('spawned', at, uid=v7(at * 1000 + 100), identity=2)]
    assert entry(events, stored=stored) == at
    # A real later exit followed by re-entry starts fresh, even delivered backwards.
    events[:0] = [event('decision_retained', NOW - 1), event('unblocked', NOW - 2)]
    assert entry(events, stored=stored) == NOW - 1


@pytest.mark.parametrize('uid', [None, 'invalid', str(uuid.UUID(int=1)), v7((NOW + 100) * 1000)])
def test_absent_or_backfilled_uid_retains_stable_timestamp_ties(uid):
    at = NOW - 601
    events = [event('spawned', at, uid=uid), event('contained', at, uid=uid)]
    assert entry(events) == at
    assert entry(list(reversed(events))) is None


@pytest.mark.parametrize('invalid', [None, True, '100', 0, -1, math.inf, math.nan])
def test_invalid_lifecycle_clock_cannot_move_before_exit_and_fabricate_age(invalid):
    events = [event('spawned', NOW - 800), event('contained', invalid),
              event('decision_retained', NOW - 700)]
    assert entry(events) is None
    assert entry(list(reversed(events))) is None


def test_local_ids_break_timestamp_ties_without_uuid_precision():
    at = NOW - 601
    events = [event('contained', at, identity=2), event('spawned', at, identity=1)]
    assert entry(events) == at


def test_consistent_straddle_precision_stays_inside_recorded_second():
    at = NOW - 601
    events = [event('contained', at, uid=v7((at + 1) * 1000 + 400)),
              event('spawned', at, uid=v7(at * 1000 + 900))]
    assert entry(events) == at
    # Timestamp is primary even when the UUID was minted across the next second.
    events.append(event('unblocked', at + 1, uid=v7((at + 1) * 1000 + 100)))
    assert entry(events) is None


def test_non_lifecycle_missing_time_does_not_poison_episode():
    events = [event('contained', NOW - 601), event('heartbeat', None)]
    assert entry(events) == NOW - 601


def test_fractional_times_and_future_clocks_preserve_existing_meaning():
    assert entry([event('contained', NOW - 601.5), event('spawned', NOW - 601.8)]) == NOW - 601.5
    row = {'status': 'triage', 'events': [event('contained', NOW + 1), event('spawned', NOW)]}
    assert attention.current_entry(row) == NOW + 1
    assert not attention.overdue(row, now=NOW)
    assert attention.next_transition(row, now=NOW) is None


@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('ahead', [20, 6 * 86400])
@pytest.mark.parametrize('repeat_age', [1, 601])
def test_future_entry_never_lets_a_later_repeated_hold_become_the_entry(stored, ahead, repeat_age):
    """Mirrors session-panel's fixture: created, a future-dated entry, then a sane repeated hold."""
    events = [event('created', 1, status='ready'), event('decision_retained', NOW + ahead),
              event('decision_retained', NOW - repeat_age)]
    entered = entry(events, stored=stored)
    assert entered is None
    for now in (NOW, NOW + 601, NOW + ahead + 601):
        assert not attention.overdue({'status': 'triage', 'events': events}, now=now)
        assert attention.next_transition({'status': 'triage', 'events': events}, now=now) is None
    # Same evidence after a real exit and re-entry starts a fresh, knowable episode.
    events += [event('unblocked', NOW + ahead + 1), event('decision_retained', NOW + ahead + 2)]
    assert entry(events, stored=stored) == NOW + ahead + 2


@pytest.mark.parametrize('stored', [False, True])
def test_future_repeated_hold_after_a_valid_entry_keeps_the_entry(stored):
    """Arrival and clock agree on who began the episode; a skewed later repeat does not reset or hide it."""
    events = [event('spawned', NOW - 900), event('contained', NOW - 601), event('decision_retained', NOW + 20)]
    assert entry(events, stored=stored) == NOW - 601
    assert attention.overdue({'status': 'triage', 'events': events}, now=NOW)


def test_same_second_hold_arriving_first_does_not_hide_the_precise_entry():
    """Only a strictly later clock is an inversion; same-second ties keep UUID/ID order."""
    at = NOW - 601
    events = [event('spawned', at - 100), event('decision_retained', at, uid=v7(at * 1000 + 800)),
              event('contained', at, uid=v7(at * 1000 + 100))]
    assert entry(events) == at


def test_rehomed_same_second_restored_holds_still_enter_before_imported_history():
    """The reported rehomed card: restored holds share the entry second and arrive first."""
    at = 1795423619
    events = [event('spend_budget_triaged', at, identity=-2), event('decision_retained', at, identity=-1),
              event('created', 1795233019, status='ready', identity=5), event('claimed', at - 31, identity=6),
              event('spawned', at - 30, identity=7)]
    assert entry(events) == at
    assert attention.overdue({'status': 'triage', 'events': events}, now=at + 601)
