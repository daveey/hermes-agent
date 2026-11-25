"""A malformed historical stop cause never aborts dispatch or readiness, and is never released by automation."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connect
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_spend_budget as support

kanban_home = support.kanban_home
CAUSES = [1, True, 1.5, [], {}, ['scope_stopped'], {'safety_refusal': True}]


def _legacy(conn, cause, status, priority=100):
    """A card that once stopped with a malformed cause and was promoted by hand afterwards."""
    tid = kb.create_task(conn, title='Legacy malformed stop', assignee='default', priority=priority)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': cause})
        kb._append_event(conn, tid, 'promoted_manual', {'actor': 'default'})
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    return tid


@pytest.mark.parametrize('cause', CAUSES, ids=repr)
def test_ready_card_with_malformed_cause_does_not_abort_dispatch(kanban_home, cause):
    with connect.connect_closing() as conn:
        bad = _legacy(conn, cause, 'ready')
        good = kb.create_task(conn, title='Healthy sibling', assignee='default', priority=1)
        spawns = support.Spawns()
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert good in spawns.ids
        assert not decision.pending(conn, bad)  # the later manual promotion decided it, as before validation


@pytest.mark.parametrize('cause', CAUSES, ids=repr)
def test_todo_card_with_malformed_cause_does_not_abort_readiness(kanban_home, cause):
    with connect.connect_closing() as conn:
        parent = kb.create_task(conn, title='Parent', assignee='default')
        bad = _legacy(conn, cause, 'todo')
        good = kb.create_task(conn, title='Healthy sibling', assignee='default', priority=1)
        kb.link_tasks(conn, parent, bad)
        kb.link_tasks(conn, parent, good)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (good,))
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        kb.recompute_ready(conn)
        spawns = support.Spawns()
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert kb.get_task(conn, good).status != 'todo' and good in spawns.ids


@pytest.mark.parametrize('cause', CAUSES, ids=repr)
def test_undecided_malformed_stop_stays_held_and_is_never_reviewed(kanban_home, cause):
    from hermes_cli import kanban_spend_reviewer as reviewer
    with connect.connect_closing() as conn:
        bad = kb.create_task(conn, title='Undecided malformed stop', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (bad,))
            kb._append_event(conn, bad, 'spend_budget_triaged', {'cause': cause})
        stop = decision.pending_episode(conn, bad)  # batch readers: held, no exception
        assert stop is not None and decision.pending(conn, bad)
        with pytest.raises(decision.MalformedStop, match='cause'):
            decision.strict_episode(conn, bad)
        episode = reviewer.episode_of(stop)
        assert not reviewer.eligible(conn, bad, episode, (True, 2))
        assert not reviewer.holds(conn, bad, episode)
        assert reviewer.context(conn, bad, episode) is None
        with kb.write_txn(conn):
            assert not reviewer.queue(conn, bad, episode, cfg=(True, 2))
            decision.ensure_producer(conn, bad)  # owner routing itself must not raise
        assert reviewer.state(conn, bad, episode) is None
        spawns = support.Spawns()
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert bad not in spawns.ids and kb.get_task(conn, bad).status == 'triage'
