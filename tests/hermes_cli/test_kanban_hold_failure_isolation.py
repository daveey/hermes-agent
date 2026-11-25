"""Every-tick own-hold repair isolates corrupt history and transient failures."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_hold_effective as effective
from hermes_cli import kanban_hold_notices as notices
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


def _held(conn, cause, *, creator=None):
    tid = kb.create_task(conn, title='held task', assignee='default', creator_task_id=creator)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': cause})
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    return tid


def _reclaim(conn):
    dispatch._run_reclaim_phase(conn, dispatch.DispatchResult(), stale_timeout_seconds=0,
                               failure_limit=2, reconcile_orphans=False, dry_run=False)


def _decisions(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']


@pytest.mark.parametrize('cause', [[], {}, 7])
def test_reclaim_survives_malformed_historical_cause(kanban_home, cause, monkeypatch, caplog):
    upkeep = []
    monkeypatch.setattr(walls, 'tick_upkeep', lambda *a, **kw: upkeep.append(True))
    with kbc.connect_closing() as conn:
        bad = _held(conn, cause)
        good = _held(conn, 'runs')
        before = kb.list_events(conn, bad)
        _reclaim(conn)
        assert upkeep == [True]
        assert len(_decisions(conn, good)) == 1
        assert kb.get_task(conn, bad).status == 'triage'
        assert kb.list_events(conn, bad) == before
        assert bad in caplog.text and 'hold retained' in caplog.text
        _reclaim(conn)
        assert upkeep == [True, True]
        assert len(_decisions(conn, good)) == 1
        assert kb.list_events(conn, bad) == before


@pytest.mark.parametrize('fault', ['read', 'previous', 'publication'])
def test_reclaim_contains_fault_rolls_back_and_retries(kanban_home, monkeypatch, caplog, fault):
    upkeep = []
    monkeypatch.setattr(walls, 'tick_upkeep', lambda *a, **kw: upkeep.append(True))
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        creator = kb.create_task(conn, title='creator', assignee='default', session_id=chat,
                                 require_scope_authority=True)
        bad = _held(conn, 'scope_stopped', creator=creator)
        good = _held(conn, 'runs')
        original = effective.current if fault == 'read' else effective.previous if fault == 'previous' else notices.publish

        def failing(live, tid, *args, **kwargs):
            if tid == bad:
                if fault == 'publication':
                    original(live, tid, *args, **kwargs)
                raise sqlite3.OperationalError('injected hold read/publication fault')
            return original(live, tid, *args, **kwargs)

        target, name = (effective, 'current') if fault == 'read' else (effective, 'previous') if fault == 'previous' else (notices, 'publish')
        monkeypatch.setattr(target, name, failing)
        before = kb.list_events(conn, bad)
        subscriptions = [tuple(r) for r in conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=?', (bad,))]
        _reclaim(conn)
        assert upkeep == [True]
        assert bad in caplog.text and 'hold retained' in caplog.text
        assert kb.get_task(conn, bad).status == 'triage'
        assert kb.list_events(conn, bad) == before
        assert len(_decisions(conn, good)) == 1
        assert [tuple(r) for r in conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=?', (bad,))] == subscriptions
        assert conn.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (creator,)).fetchone()[0] == 0
        assert not conn.in_transaction
        monkeypatch.setattr(target, name, original)
        _reclaim(conn)
        _reclaim(conn)
        assert upkeep == [True, True, True]
        assert len(_decisions(conn, bad)) == len(_decisions(conn, good)) == 1
        assert kb.get_task(conn, bad).status == 'triage'
        assert conn.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (creator,)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (bad,)).fetchone()[0] == 0
