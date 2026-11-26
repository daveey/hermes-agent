"""Registered-check carrier selection and writer custody races. [daveey]"""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_idle_candidates as idle
from hermes_cli import kanban_sweep_gate as gate
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_home_shared_consumer_custody as support

consumer_board = support.consumer_board


def evidence(c):
    return [r[0] for r in c.execute('SELECT task_id FROM task_events WHERE kind=?', (checks.CHECK_EVENT,))]


def events(c, tid):
    return [tuple(r) for r in c.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id', (tid,))]


@pytest.mark.parametrize('phase', ['pending', 'prepared', 'ordinary'])
@pytest.mark.parametrize('race', [False, True])
def test_checks_frozen_carrier_does_not_starve_peer(consumer_board, monkeypatch, race, phase):
    c = consumer_board
    for tid in ('t_A', 't_B'):
        support.card(c, tid)
        support.wall(c, tid, 'wall-' + tid.lower())
    calls, frozen_events = [], []

    def run(command):
        calls.append(command)
        if race and command == 'check-a':
            support.freeze(c, 't_A', phase)
            frozen_events[:] = events(c, 't_A')
        return 0, 'ok'

    monkeypatch.setattr(checks, '_run_check', run)
    monkeypatch.setattr(checks, 'registered_checks', lambda cfg=None: {'wall-t_a': 'check-a', 'wall-t_b': 'check-b'})
    if not race:
        support.freeze(c, 't_A', phase)
        frozen_events[:] = events(c, 't_A')
    result = SimpleNamespace()
    gate.reset()
    idle.sweep_shared_blockers(c, result)
    assert 't_B' in evidence(c), (calls, evidence(c), vars(result))
    assert ('t_A' in evidence(c)) == (phase == 'ordinary')
    if phase != 'ordinary':
        assert events(c, 't_A') == frozen_events
        assert kb.get_task(c, 't_A').status == 'blocked'
        assert ('check-a' in calls) == race  # preselection exclusions perform no I/O


@pytest.mark.parametrize('phase', ['pending', 'prepared'])
@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('rc', [0, 1, 'error'])
def test_same_wall_reselects_healthy_carrier(consumer_board, monkeypatch, phase, race, rc):
    c = consumer_board
    for tid in ('t_A', 't_B'):
        support.card(c, tid)
        support.wall(c, tid, 'wall-a')
    before = []
    if not race:
        support.freeze(c, 't_A', phase)
        before[:] = events(c, 't_A')
    calls = []

    def run(command):
        calls.append(command)
        if race:
            support.freeze(c, 't_A', phase)
            before[:] = events(c, 't_A')
        return rc, 'result'

    monkeypatch.setattr(checks, '_run_check', run)
    monkeypatch.setattr(checks, 'registered_checks', lambda cfg=None: {'wall-a': 'check-a'})
    due = []
    assert checks.run_due_checks(c, due_out=due) == (['wall-a'] if rc == 0 else [])
    assert calls == ['check-a']
    assert evidence(c) == ['t_B']
    assert events(c, 't_A') == before
    payload = kb._json_dict(c.execute('SELECT payload FROM task_events WHERE kind=?', (checks.CHECK_EVENT,)).fetchone()[0])
    assert payload['rc'] == rc
    assert payload['fails'] == (1 if rc == 'error' else 0)
    assert bool(due) == (rc != 0)


@pytest.mark.parametrize('phase', ['pending', 'prepared'])
def test_raced_frozen_carrier_discards_changed_assertion_without_starving_next_wall(consumer_board, monkeypatch, phase):
    c = consumer_board
    for tid, bid in [('t_A', 'wall-a'), ('t_B', 'wall-a'), ('t_C', 'wall-c')]:
        support.card(c, tid)
        support.wall(c, tid, bid)
    before = []
    def run(command):
        if command == 'check-a':
            # New assertion changes the cohort while the check is out of lock.
            support.card(c, 't_new')
            support.wall(c, 't_new', 'wall-a')
            support.freeze(c, 't_A', phase)
            before[:] = events(c, 't_A')
        return 0, 'ok'
    monkeypatch.setattr(checks, '_run_check', run)
    monkeypatch.setattr(checks, 'registered_checks', lambda cfg=None: {'wall-a': 'check-a', 'wall-c': 'check-c'})
    checks.run_due_checks(c)
    assert evidence(c) == ['t_C']
    assert events(c, 't_A') == before
    assert c.execute('SELECT task_id FROM task_events WHERE kind=?', (checks.ATTEMPT_EVENT,)).fetchone()[0] == 't_B'
