"""Operator releases and long-running work must not starve cheap-check walls."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


def test_operator_unblock_survives_peer_running_and_reassertion(kanban_home, monkeypatch):
    monkeypatch.setattr(checks, 'registered_checks', lambda cfg=None: {'test-wall': 'exit 1'})
    with kbc.connect_closing() as conn:
        target = helpers._blocked_behind(conn, 'target', blocker_id='test-wall')
        peer = helpers._blocked_behind(conn, 'long landing', blocker_id='test-wall')
        assert kb.unblock_task(conn, peer) and kb.claim_task(conn, peer)
        assert kb.unblock_task(conn, target)
        helpers._blocked_behind(conn, 'another assertion', blocker_id='test-wall')
        assert blockers.shared_blocker_guard(conn, target) is None
        # An explicit unblock is consumed by a fresh assertion from this card.
        assert kb.claim_task(conn, target)
        assert kb.block_task(conn, target, reason='wall remains', kind='capability', blocker_id='test-wall')
        assert blockers.shared_blocker_guard(conn, target) == blockers.GUARD_REASON


def test_cheap_check_releases_waiter_without_waiting_for_long_run(kanban_home):
    with kbc.connect_closing() as conn:
        target = helpers._blocked_behind(conn, 'target', blocker_id='test-wall')
        peer = helpers._blocked_behind(conn, 'long landing', blocker_id='test-wall')
        assert kb.unblock_task(conn, peer) and kb.claim_task(conn, peer)
        now = blockers.blocker_states(conn)['test-wall'].asserted_at + 10
        assert checks.run_due_checks(conn, cfg={'kanban': {'shared_blocker_checks': {'test-wall': 'exit 0'}}}, now=now) == ['test-wall']
        assert target in blockers.release_cleared_blockers(conn, now=now)
        assert kb.get_task(conn, peer).status == 'running'
        assert blockers.shared_blocker_guard(conn, target, now=now) is None
