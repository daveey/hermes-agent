"""An impossible CI producer must reach repair, not reserve its last run. [daveey]"""
import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_ci_progress_pr as helpers

kanban_home = helpers.kanban_home
api = helpers.api


@pytest.mark.parametrize('cause', ['conflict', 'offline_runners'])
@pytest.mark.parametrize('spent', [False, True])
def test_last_authorized_run_reaches_ci_producer_repair(kanban_home, api, monkeypatch, cause, spent):
    with kbc.connect_closing() as conn:
        tid = helpers._parked(conn, monkeypatch)
        if spent:
            with kb.write_txn(conn):
                conn.execute('UPDATE task_runs SET started_at=ended_at-120 WHERE task_id=?', (tid,))
        budget.set_override(conn, tid, runs=2 if spent else 1, by='owner', reason='one repair run left')
        before = budget.spend(conn, tid).runs
        assert before == (1 if spent else 0)
        if cause == 'conflict':
            api.dirty()
        else:
            api.run('queued', age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert kb.get_task(conn, tid).status == 'ready'
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert 'ci_run_unstartable' in kinds
        assert 'terminal_run_reserved' not in kinds
        assert helpers._notes(conn, tid)
        assert budget.spend(conn, tid).runs == before
        assert budget.card_limits(conn, tid, budget.limits()).runs == (2 if spent else 1)
        assert budget.hold(conn, tid) is None
