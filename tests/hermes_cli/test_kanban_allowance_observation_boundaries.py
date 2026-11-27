"""Unmask individual observation validation predicates; malformed diagnostics never grant authority."""
from dataclasses import replace

import pytest

from hermes_cli import kanban_allowance_observation as observation
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_allowance_observation as reads
from tests.hermes_cli import test_kanban_allowance_supersession as supersession
from tests.hermes_cli import test_kanban_spend_allowance as fixtures

kanban_home = fixtures.kanban_home
primary = fixtures.primary


@pytest.mark.parametrize('changes', [
    {'runs': 10, 'used': 10, 'remaining': False},
    {'runs': 10, 'used': True, 'remaining': 9},
    {'runs': 10.0, 'used': 10, 'remaining': 0},
    {'runs': 11, 'used': 11, 'remaining': 0},
    {'runs': 10, 'used': -1, 'remaining': 11},
    {'runs': 10, 'used': 11, 'remaining': -1},
    {'home': 7}, {'episode': True},
])
def test_consistent_arithmetic_does_not_mask_invalid_types_or_bounds(primary, monkeypatch, changes):
    tid, uid = fixtures._released(primary)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, **changes))
    assert observation.read(primary, tid) is None


def test_wrong_outer_board_with_valid_allowance_is_unknown(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    reply = reads._reply(primary, tid, uid)
    reply['board'] = 'wrong'
    reads._hub(monkeypatch, primary, reply)
    assert observation.read(primary, tid) is None


@pytest.mark.parametrize('data', [[], 'active', 7])
def test_board_state_nonobject_is_unknown_not_an_exception(primary, monkeypatch, data):
    tid, _ = fixtures._released(primary)
    reads._hub(monkeypatch, primary, {'board': fixtures.sync.sync_state(primary)['board_id'], 'allowance': data})
    assert observation.board_state(primary, tid) is None


def test_board_state_missing_allowance_key_with_correct_board_is_unknown(primary, monkeypatch):
    tid, _ = fixtures._released(primary)
    reads._hub(monkeypatch, primary, {'board': fixtures.sync.sync_state(primary)['board_id']})
    assert observation.board_state(primary, tid) is None


def test_missing_observation_and_budget_have_explicit_empty_results(primary):
    tid = kb.create_task(primary, title='No allowance', assignee='default')
    assert observation.current(primary, tid, None) is False
    assert observation._budget(primary, tid) is None


def test_missing_sync_identity_after_expected_preflight_never_opens_transport(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    calls = reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid))
    real = fixtures.allowance.expected

    def losing_identity(conn, task):
        want = real(conn, task)
        assert want is not None
        with fixtures.kbc.write_txn(conn):
            conn.execute('DELETE FROM kanban_sync_state')
        return want

    monkeypatch.setattr(fixtures.allowance, 'expected', losing_identity)
    assert observation.read(primary, tid) is None
    assert calls == []


def test_observation_current_requires_exact_board_and_surviving_identity(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid))
    seen = observation.read(primary, tid)
    assert seen is not None
    assert observation.current(primary, tid, seen)
    assert not observation.current(primary, tid, replace(seen, board='wrong'))
    with fixtures.kbc.write_txn(primary):
        primary.execute('DELETE FROM kanban_sync_state')
    assert not observation.current(primary, tid, seen)


def test_stuck_observation_loses_identity_without_exception(primary, monkeypatch):
    tid, _, _ = supersession._decided(primary, monkeypatch)
    seen = observation.stuck(primary, tid)
    assert seen is not None and observation.still_stuck(primary, tid, seen)
    with fixtures.kbc.write_txn(primary):
        primary.execute('DELETE FROM kanban_sync_state')
    assert observation.still_stuck(primary, tid, seen) is False
