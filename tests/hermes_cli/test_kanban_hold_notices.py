"""Fresh holds must produce decisions before the stale-triage/capacity filters."""
import contextlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('cause', ['runs', 'scope_stopped'])
def test_fresh_hold_notifies_chat_and_creator_once(kanban_home, cause, monkeypatch):
    from hermes_cli import kanban_hold_notices as notices
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        creator = kb.create_task(conn, title='creator', assignee='default', session_id=chat,
                                 require_scope_authority=True)
        tid = kb.create_task(conn, title='lander', assignee='default', creator_task_id=creator)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': cause, 'reason': 'window ended'})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        notices.sweep(conn)
        notices.sweep(conn)
        events = [e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']
        assert len(events) == 1
        sub = conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=? AND chat_id=?', (tid, chat)).fetchone()
        assert sub is not None and sub['last_event_id'] < events[0].id
        comments = conn.execute('SELECT body FROM task_comments WHERE task_id=?', (creator,)).fetchall()
        assert len(comments) == 1 and tid in comments[0][0] and 'window ended' in comments[0][0]
        assert kb.get_task(conn, tid).status == 'triage'
        from hermes_state import SessionDB
        from tui_gateway import kanban_unloaded as ku
        monkeypatch.setenv('HERMES_KANBAN_UNLOADED_WAKE', '0')
        with SessionDB(kanban_home / 'state.db') as db:
            @contextlib.contextmanager
            def opener(sid):
                yield db, sid
            # Production grace must not delay a fresh decision notice.
            delivered = ku.sweep_once(set(), session_db=opener)
            assert tid in [item['task_id'] for item in delivered]
            assert next(s for s in db.list_sessions_rich() if s['id'] == chat)['unread']
            assert ku.sweep_once(set(), grace_s=0, session_db=opener) == []
        # A new episode gets a new decision, not a once-per-card latch.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': cause, 'reason': 'next window'})
        notices.sweep(conn)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']) == 2


def test_cap_one_spent_generic_wake_has_decision_before_late_terminal(kanban_home, tmp_path):
    import time
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_hold_notices as notices
    from hermes_cli import kanban_wake_check as wake
    from tests.hermes_cli.test_kanban_wake_check import _running, _probe
    flag = tmp_path / 'bb-land-terminal'
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        chat = helpers._session()
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET session_id=? WHERE id=?', (chat, tid))
            conn.execute('UPDATE task_runs SET started_at=? WHERE task_id=?', (int(time.time()) - 120, tid))
        budget.set_override(conn, tid, runs=1, by='owner', reason='bounded lander')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z',
                                wake_check=_probe(flag), check_every=120)
        # A real closed >=60-second run counts, not a short non-spending fixture run.
        with kb.write_txn(conn):
            conn.execute('UPDATE task_runs SET ended_at=? WHERE task_id=?', (int(time.time()) - 1, tid))
            wake._wake(conn, tid, 'ci_run_unstartable', {'reason': 'generic status'})
        assert budget.spend(conn, tid).runs == 1
        assert budget.hold(conn, tid) is not None
        notices.sweep(conn)
        assert kb.get_task(conn, tid).status == 'triage'
        signals = [e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']
        assert len(signals) == 1
        flag.write_text('bb land: exit 0\n')
        notices.sweep(conn)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']) == 1
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 1
        from hermes_cli.kanban_spend_decision import pending
        assert pending(conn, tid)  # the existing stop, not a reset allowance, still gates admission




