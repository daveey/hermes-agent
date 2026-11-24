"""Own-hold polling keeps recipient arrival, writer and lifecycle custody separate."""
import contextlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_hold_notices as notices
from tests.hermes_cli import test_kanban_local_adoption as adoption

world = adoption.world


def _stop(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'window ended'})


def test_real_late_adoption_gets_existing_own_hold(world, capsys):
    conn, tid, _ = world
    with kb.write_txn(conn):
        conn.execute('DELETE FROM kanban_notify_subs WHERE task_id=?', (tid,))
    _stop(conn, tid)
    notices.sweep(conn)
    first = next(e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready')
    assert adoption.adopt(tid, capsys)['adopted']
    notices.sweep(conn)
    from hermes_cli.kanban_db_notify import unseen_events_for_sub
    _, pending = unseen_events_for_sub(conn, task_id=tid, platform='tui', chat_id='chat',
                                       kinds=['spend_budget_decision_ready'])
    assert pending and pending[-1].id > first.id
    assert pending[-1].payload['authority']['chats']
    assert kb.get_task(conn, tid).status == 'triage'


def test_unchanged_own_hold_takes_no_writer_or_duplicate_handoff(world, capsys):
    conn, tid, _ = world
    assert adoption.adopt(tid, capsys)['adopted']
    _stop(conn, tid)
    notices.sweep(conn)
    from hermes_cli import kanban_spend_decision as decisions
    decisions.ensure_producer(conn, tid)
    before = conn.total_changes
    trace = []
    conn.set_trace_callback(trace.append)
    try:
        notices.sweep(conn)
        decisions.ensure_producer(conn, tid)
    finally:
        conn.set_trace_callback(None)
    assert conn.total_changes == before
    assert not [q for q in trace if q.upper().startswith(('BEGIN', 'UPDATE', 'INSERT', 'DELETE'))], trace
    assert len([e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']) == 1


@pytest.mark.parametrize('terminal', ['ready', 'done', 'archived'])
def test_released_during_writer_acquisition_never_publishes_stale_hold(world, monkeypatch, terminal):
    conn, tid, _ = world
    _stop(conn, tid)
    original = kb.write_txn
    entered = []

    @contextlib.contextmanager
    def changing(db, **kwargs):
        entered.append(True)
        with original(db):
            db.execute('UPDATE tasks SET status=? WHERE id=?', (terminal, tid))
        with original(db, **kwargs):
            yield db

    monkeypatch.setattr(kb, 'write_txn', changing)
    notices.sweep(conn)
    assert entered
    assert not [e for e in kb.list_events(conn, tid) if e.kind == 'spend_budget_decision_ready']
    assert kb.get_task(conn, tid).status == terminal
