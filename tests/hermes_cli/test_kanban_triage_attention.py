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


# Every real triage writer's entry event, as the writer records it (kind + payload). Each starts a card that
# was created ready, claimed and then blocked, so a missed entry keeps the prior non-triage phase.
_WRITER_ENTRIES = [
    ('kanban_block_policy recheck sweep', 'ask_rechecked', {}),
    ('kanban_block_policy agent escalation', 'agent_block_escalated', {}),
    ('kanban_refusal_fallback pending spend decision', 'decision_hold_restricted', {'episode': 'e1'}),
    ('kanban_protect_stopped', 'decision_protected_stopped', {'version': 1}),
    ('kanban_stop_ended local-shadow stop', 'administrative_stop_restricted', {'version': 1}),
    ('kanban_retain', 'decision_retained', {'status': 'triage'}),
    ('kanban_spend_budget over budget', 'spend_budget_triaged', {}),
    ('kanban_spend_budget pending decision', 'spend_budget_held', {'status': 'triage'}),
    ('kanban_no_progress', 'no_progress_escalated', {'status': 'triage'}),
    ('kanban_block_routing', 'supervisor_escalated', {}),
    ('kanban_failure_accounting / review admission', 'gave_up', {'reason_code': 'worker_failures_exhausted'}),
    ('kanban_containment_lifecycle', 'contained', {'version': 1}),
    ('route_block loop', 'block_loop_detected', {'status': 'triage'}),
    ('block_task exhausted budget / source refusal block', 'blocked', {'status': 'triage'}),
    ('legacy block_task re-kind', 'blocked', {'rekind_reason': 'continuation_exhausted'}),
    ('kanban_transfer import of a dir/worktree card', 'imported', {'board': 'b', 'parked': True}),
]


def _history(kind, payload, at):
    return [event('created', 1, status='ready'), event('claimed', 2), event('blocked', 3),
            event(kind, at, **payload)]


@pytest.mark.parametrize('label,kind,payload', _WRITER_ENTRIES, ids=[w[0] for w in _WRITER_ENTRIES])
def test_every_real_triage_writer_starts_an_episode(label, kind, payload):
    row = {'status': 'triage', 'events': _history(kind, payload, NOW - 7200)}
    assert attention.current_entry(row) == NOW - 7200, label
    assert attention.overdue(row, now=NOW)
    assert not attention.overdue({**row, 'events': _history(kind, payload, NOW - 600)}, now=NOW)


@pytest.mark.parametrize('kind,payload', [
    ('blocked', {'status': 'blocked'}), ('blocked', {}), ('imported', {'parked': False}),
    ('continued', {'status': 'ready'}), ('condition_met', {'status': 'ready'}),
    ('dependency_wait', {}),
])
def test_recorded_landing_wins_over_kind_default(kind, payload):
    events = [event('contained', NOW - 7200), event(kind, NOW - 3000, **payload),
              event('decision_retained', NOW - 30)]
    assert attention.current_entry({'status': 'triage', 'events': events}) == (
        NOW - 7200 if kind == 'imported' else NOW - 30)


def test_annotate_reads_every_writer_entry_from_the_event_table():
    import json
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, '
                 'payload TEXT, created_at INTEGER)')
    rows = []
    for index, (_, kind, payload) in enumerate(_WRITER_ENTRIES):
        tid = f't{index}'
        rows.append({'id': tid, 'status': 'triage'})
        for item in _history(kind, payload, NOW - 7200):
            conn.execute('INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)',
                         (tid, item['kind'], json.dumps(item['payload']), item['created_at']))
    annotated = attention.annotate(conn, rows)
    assert [row['triage_entered_at'] for row in annotated] == [NOW - 7200] * len(_WRITER_ENTRIES)


def test_literal_triage_writers_are_all_registered():
    """A new ``SET status='triage'`` writer must add its entry event here (and to the map) or this fails."""
    import pathlib
    import re
    root = pathlib.Path(attention.__file__).resolve().parent
    writers = {path.name for path in root.glob('*.py')
               if re.search(r"SET status\s*=\s*'triage'", path.read_text(encoding='utf-8'))}
    registered = {
        'kanban_allowance_maintenance.py', 'kanban_block_policy.py', 'kanban_block_routing.py',
        'kanban_containment_lifecycle.py', 'kanban_failure_accounting.py', 'kanban_no_progress.py',
        'kanban_protect_stopped.py', 'kanban_refusal_fallback.py', 'kanban_retain.py',
        'kanban_review_admission.py', 'kanban_spend_budget.py', 'kanban_stop_ended.py', 'kanban_transfer.py',
    }
    assert writers == registered
    for _, kind, payload in _WRITER_ENTRIES:
        assert attention.landing(kind, payload) == 'triage'
        assert kind in attention.LIFECYCLE_KINDS


def test_annotate_costs_one_statement_with_triage_cards_and_none_without():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)')
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t1', 'contained', '{}', ?)", (NOW - 700,))
    statements = []
    conn.set_trace_callback(statements.append)
    rows = attention.annotate(conn, [{'id': 't1', 'status': 'triage'}, {'id': 't2', 'status': 'ready'}])
    assert [row['triage_entered_at'] for row in rows] == [NOW - 700, None]
    assert len(statements) == 1, statements
    statements.clear()
    assert attention.annotate(conn, [{'id': 't2', 'status': 'ready'}])[0]['triage_entered_at'] is None
    assert statements == []


def test_annotate_without_an_event_table_is_unknown_not_aged():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    assert attention.annotate(conn, [{'id': 't1', 'status': 'triage'}])[0]['triage_entered_at'] is None


@pytest.mark.parametrize('age,red', [(599, False), (600, False), (601, True), (6 * 86400, True)])
def test_an_explicitly_stopped_retained_scope_ages_like_every_other_triage_cause(age, red):
    row = {'status': 'triage', 'triage_entered_at': NOW - age, 'retained_stop': {'reason': 'scope_stopped'}}
    assert attention.overdue(row, now=NOW) is red
    assert (attention.next_transition(row, now=NOW) is None) is red
