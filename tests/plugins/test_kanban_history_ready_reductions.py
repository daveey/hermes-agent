"""Ready/blocked history reductions retain live run dependencies."""
from types import MappingProxyType

import pytest

from hermes_cli import kanban_diagnostics as kd
from hermes_cli.kanban_diagnostic_history import EventHistory


@pytest.mark.parametrize('status', ['ready', 'blocked', 'todo', 'running'])
def test_warm_history_avoids_full_scans(monkeypatch, status):
    events = EventHistory([{'kind': 'progress', 'created_at': 1}] * 3000)
    task = {'id': 't', 'status': status, 'created_at': 1, 'assignee': 'worker'}
    expected = kd.compute_task_diagnostics(task, list(events), [], now=1000)
    assert kd.compute_task_diagnostics(task, events, [], now=1000) == expected
    calls = []
    real = kd._event_kind
    def traced(ev):
        calls.append(ev)
        return real(ev)
    monkeypatch.setattr(kd, '_event_kind', traced)
    assert kd.compute_task_diagnostics(task, events, [], now=1000) == expected
    assert len(calls) < 10


@pytest.mark.parametrize('field', ['worker_pid', 'claim_lock', 'worker_started_at', 'last_heartbeat_at'])
def test_ready_floor_tracks_run_nullness_and_return_to_prior_value(field):
    events = EventHistory([
        {'kind': 'unblocked', 'created_at': 10},
        {'kind': 'claimed', 'created_at': 100, 'run_id': 7},
        {'kind': 'released', 'created_at': 200, 'run_id': 7, 'payload': '{"unused_grant":true}'},
    ])
    base = {'id': 7, 'status': 'released', 'outcome': 'released', 'ended_at': 200,
            'metadata': '{"unused_grant":true}'}
    for value, expected in [(None, 10), (0, 200), (None, 10)]:
        runs = (MappingProxyType(dict(base, **{field: value})),)
        assert kd._last_ready_entry_ts(list(events), runs) == expected
        assert kd._last_ready_entry_ts(events, runs) == expected


@pytest.mark.parametrize('replacement', [False, True])
def test_ready_floor_mutable_metadata_and_bool_int_distinction(replacement):
    events = EventHistory([
        {'kind': 'unblocked', 'created_at': 10},
        {'kind': 'claimed', 'created_at': 100, 'run_id': 7},
        {'kind': 'released', 'created_at': 200, 'run_id': 7, 'payload': '{"unused_grant":true}'},
    ])
    run = {'id': 7, 'status': 'released', 'outcome': 'released', 'ended_at': 200,
           'metadata': {'unused_grant': True}}
    assert kd._last_ready_entry_ts(events, [run]) == 10
    if replacement:
        run['metadata'] = {'unused_grant': 1}
    else:
        run['metadata']['unused_grant'] = False
    assert kd._last_ready_entry_ts(list(events), [run]) == 200
    assert kd._last_ready_entry_ts(events, [run]) == 200


def test_ready_floor_key_distinguishes_equal_values_of_other_types():
    events = EventHistory([{'kind': 'unblocked', 'created_at': 10}])
    for end in (300, 300.0, 300, True, 1):
        runs = (MappingProxyType({'id': 7, 'status': 'done', 'outcome': 'completed', 'ended_at': end}),)
        expected = kd._last_ready_entry_ts(list(events), runs)
        actual = kd._last_ready_entry_ts(events, runs)
        assert (actual, type(actual)) == (expected, type(expected))


def test_assigned_ready_run_mutations_change_visible_diagnostic():
    events = EventHistory([{'kind': 'unblocked', 'created_at': 10}])
    task = {'id': 't', 'status': 'ready', 'assignee': 'worker', 'created_at': 1}
    runs = []
    previous = kd.compute_task_diagnostics(task, events, runs, now=10000)
    for end in (100, 200, 'bad', 300, None):
        runs = [{'id': 1, 'ended_at': end}]
        actual = kd.compute_task_diagnostics(task, events, runs, now=10000)
        expected = kd.compute_task_diagnostics(task, list(events), runs, now=10000)
        assert actual == expected
        assert actual != previous
        previous = actual
    assert kd.compute_task_diagnostics(task, events, [], now=10000) == kd.compute_task_diagnostics(
        task, list(events), [], now=10000)


def test_non_mapping_runs_use_original_path():
    from types import SimpleNamespace
    events = EventHistory([{'kind': 'unblocked', 'created_at': 10}])
    run = SimpleNamespace(id=1, ended_at=20)
    assert kd._last_ready_entry_ts(events, [run]) == 20
    run.ended_at = 30
    assert kd._last_ready_entry_ts(events, [run]) == 30


def test_terminal_provider_and_block_arrival_order():
    for last in ('gave_up', 'claimed', 'completed', 'promoted', 'unblocked'):
        events = EventHistory([
            {'kind': 'gave_up', 'payload': '{"terminal_provider":true}', 'created_at': 100},
            {'kind': last, 'created_at': 1, 'payload': '{"terminal_provider":true}'},
        ])
        assert kd._latest_gave_up_is_terminal_provider(events) == (last == 'gave_up')
    events = EventHistory([
        {'kind': 'blocked', 'created_at': 100, 'payload': '{}'},
        {'kind': 'blocked', 'created_at': 10, 'payload': '{"reason":"review-required: check"}'},
    ])
    task = {'id': 't', 'status': 'blocked'}
    cfg = {'_graph': {'children': [{'id': 'child', 'status': 'todo'}]}}
    for _ in range(2):
        actual = kd._rule_review_dependency_deadlock(task, events, [], 1000, cfg)
        assert actual and actual == kd._rule_review_dependency_deadlock(task, list(events), [], 1000, cfg)


def test_history_replacement_and_rule_error_isolation():
    for rows in [
        [{'kind': 'blocked', 'created_at': 30}, {'kind': 'blocked', 'created_at': 10}],
        [{'kind': 'blocked', 'created_at': 'bad'}],
        [{'kind': 'gave_up', 'created_at': 20, 'payload': '{"terminal_provider":true}'}],
    ]:
        events = EventHistory(rows)
        for status in ('ready', 'blocked', 'todo'):
            task = {'id': 't', 'status': status, 'consecutive_failures': 5}
            for now in (100, 100000, 50):
                assert kd.compute_task_diagnostics(task, events, [], now=now) == kd.compute_task_diagnostics(
                    task, list(events), [], now=now)
