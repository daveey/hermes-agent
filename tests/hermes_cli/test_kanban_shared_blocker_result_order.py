"""Equal-second check receipts must not depend on replica-local event IDs."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('bid', ['test-wall', 'github-rest-quota'])
@pytest.mark.parametrize('reverse', [False, True])
def test_equal_second_failure_wins_over_success(kanban_home, reverse, bid):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_behind(conn, 'wall', blocker_id=bid)
        now = blockers.blocker_states(conn)[bid].asserted_at + 10
        token = checks._assertion_tokens(conn, now)[bid]
        results = [0, 1]
        if reverse:
            results.reverse()
        with kb.write_txn(conn):
            for rc in results:
                kb._append_event(conn, tid, checks.CHECK_EVENT, {
                    'blocker_id': bid, 'checked_at': now, 'started_at': now,
                    'assertion_id': token, 'command': 'probe', 'rc': rc, 'fails': 0,
                })
        assert checks._latest_results(conn, now)[bid]['rc'] == 1
        assert blockers.blocker_states(conn, now=now)[bid].is_open
        assert blockers.release_cleared_blockers(conn, now=now) == []


@pytest.mark.parametrize('section', ['invalid', ['invalid'], 7])
def test_malformed_section_disables_checks(section):
    assert checks.registered_checks({'kanban': section}) == {}
