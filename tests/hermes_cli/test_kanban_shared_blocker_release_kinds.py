"""Only an operator's release survives a newer assertion of a cheap-check wall.

The dispatcher writes the same ``unblocked`` event when it releases cards itself
(``release_cleared_blockers`` after a clear, ``probe_stranded_blockers`` for a
stranded wall). Those releases must not earn the operator exemption: once a run
that started after the release hits the wall again, the released cards are held
like any other card behind it, so each one does not spawn just to rediscover it.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home

WALL = 'github-rest-quota'
CFG = {'kanban': {'shared_blocker_checks': {WALL: 'exit 0'}}}


@pytest.fixture
def clock(monkeypatch):
    now = [1800000000]
    monkeypatch.setattr(blockers.time, 'time', lambda: now[0])
    monkeypatch.setattr(checks, 'registered_checks', lambda cfg=None: {WALL: 'exit 0'})
    return now


def _check_release(conn, clock):
    """Three cards behind the wall; the registered check exits 0 and the dispatcher releases them."""
    cards = []
    for i in range(3):
        cards.append(helpers._legacy_blocked_behind(conn, f'waiter {i}', blocker_id=WALL))
        clock[0] += 1
    clock[0] += 60
    assert checks.run_due_checks(conn, cfg=CFG) == [WALL]
    clock[0] += 30
    assert sorted(blockers.release_cleared_blockers(conn)) == sorted(cards)
    for tid in cards:
        assert kb.get_task(conn, tid).status == 'ready'
        assert blockers.shared_blocker_guard(conn, tid) is None
    return cards


def test_check_released_cards_held_by_post_recovery_assertion(kanban_home, clock):
    with kbc.connect_closing() as conn:
        cards = _check_release(conn, clock)
        clock[0] += 60
        # A run that started after the recovery finds the quota exhausted again.
        helpers._blocked_behind(conn, 'fresh run hits the wall again', blocker_id=WALL)
        assert blockers.blocker_states(conn)[WALL].is_open
        for tid in cards:
            assert blockers.shared_blocker_guard(conn, tid) == blockers.GUARD_REASON


def test_check_released_cards_ignore_stale_pre_recovery_assertion(kanban_home, clock):
    """#2923's fence: a run that began before the recovery never re-holds a released card."""
    with kbc.connect_closing() as conn:
        stale = kb.create_task(conn, title='already running with old context', assignee='default')
        assert kb.claim_task(conn, stale)
        clock[0] += 1
        cards = _check_release(conn, clock)
        clock[0] += 60
        assert kb.block_task(conn, stale, reason='stale quota context', kind='capability', blocker_id=WALL)
        assert not blockers.blocker_states(conn)[WALL].is_open
        for tid in cards:
            assert blockers.shared_blocker_guard(conn, tid) is None


def test_probe_released_card_held_by_newer_assertion(kanban_home, clock):
    with kbc.connect_closing() as conn:
        cards = []
        for i in range(3):
            cards.append(helpers._legacy_blocked_behind(conn, f'stranded {i}', blocker_id=WALL))
            clock[0] += 1
        clock[0] += 1800
        probe, peer = cards[0], cards[1]
        assert blockers.probe_stranded_blockers(conn, interval=1800) == [probe]
        assert kb.get_task(conn, probe).status == 'ready'
        assert blockers.shared_blocker_guard(conn, probe, interval=1800) is None
        clock[0] += 30
        # A peer's run that started after the release (its own wake) hits the wall again.
        assert kb.unblock_task(conn, peer) and kb.claim_task(conn, peer)
        clock[0] += 30
        assert kb.block_task(conn, peer, reason='quota still out', kind='capability', blocker_id=WALL)
        assert blockers.shared_blocker_guard(conn, probe, interval=1800) == blockers.GUARD_REASON


def test_operator_release_after_dispatcher_release_stays_exempt(kanban_home, clock):
    """An operator's unblock newer than a dispatcher release is still the operator's."""
    with kbc.connect_closing() as conn:
        cards = _check_release(conn, clock)
        target = cards[0]
        clock[0] += 10
        assert kb.claim_task(conn, target)
        assert kb.block_task(conn, target, reason='still walled', kind='capability', blocker_id=WALL)
        clock[0] += 10
        assert kb.unblock_task(conn, target)   # the operator says: try again now
        clock[0] += 10
        helpers._blocked_behind(conn, 'peer reasserts', blocker_id=WALL)
        assert blockers.blocker_states(conn)[WALL].is_open
        assert blockers.shared_blocker_guard(conn, target) is None


def test_operator_release_then_hold_retire_clear_stays_exempt(kanban_home, clock):
    """A clear that only retires a ready card's historical hold is not a dispatcher release.

    ``release_cleared_blockers`` appends :data:`CLEARED_EVENT` (no ``woke``) to cards already
    ``ready`` when the wall clears; that annotation must not turn the operator's earlier unblock
    into the dispatcher's, or a peer's newer assertion would hold the operator-released card.
    """
    with kbc.connect_closing() as conn:
        # The check's evidence lands on the lowest card id, so the lower id plays the parked peer
        # and the clear's annotation is the target's very next event after the operator's unblock.
        pair = [helpers._legacy_blocked_behind(conn, f'waiter {i}', blocker_id=WALL) for i in range(2)]
        peer, target = sorted(pair)
        clock[0] += 10
        assert kb.unblock_task(conn, target)   # the operator says: try again now
        assert kb.get_task(conn, target).status == 'ready'
        clock[0] += 60
        assert checks.run_due_checks(conn, cfg=CFG) == [WALL]
        clock[0] += 30
        assert blockers.release_cleared_blockers(conn) == [peer]
        kinds = [r['kind'] for r in conn.execute(
            'SELECT kind FROM task_events WHERE task_id=? ORDER BY id', (target,))]
        assert kinds[-2:] == ['unblocked', blockers.CLEARED_EVENT]
        clock[0] += 60
        helpers._blocked_behind(conn, 'fresh run hits the wall again', blocker_id=WALL)
        assert blockers.blocker_states(conn)[WALL].is_open
        assert blockers._operator_released(conn, target, WALL) is True
        assert blockers.shared_blocker_guard(conn, target) is None
