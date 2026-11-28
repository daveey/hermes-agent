"""The ninth attempt creates a prerequisite rather than a sticky replan hold."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_auto_replan as replan
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _card(conn, n=8, **kw):
    tid = kb.create_task(conn, title='Nonconverging goal', body='Do not weaken acceptance.', assignee='default', **kw)
    helpers._runs(conn, tid, n, usage={'cache_read_tokens': 1})
    return tid


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_dispatch_creates_one_parent_before_ninth_run(kanban_home, lane):
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, tid))
        spawns = helpers.Spawns()
        dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert tid not in spawns.ids
        assert len(kb.list_runs(conn, tid)) == 8
        assert kb.get_task(conn, tid).status == 'todo'
        parent, = kb.parent_ids(conn, tid)
        task = kb.get_task(conn, parent)
        assert task.max_retries == 3
        assert 'Do not weaken acceptance' in task.body
        assert task.idempotency_key == f'replan:{tid}:initial'
        dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert kb.parent_ids(conn, tid) == [parent]
        assert kb.complete_task(conn, parent, result='New plan recorded on original')
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == lane
        assert replan.hold(conn, tid) is None  # Home publishes the portable plan before admission.
        assert 'Do not weaken acceptance.' in kb.get_task(conn, tid).body
        assert 'New plan recorded on original' in kb.get_task(conn, tid).body
        assert replan.state(conn, tid).count == 0


def test_eighth_allowed_ninth_claim_refused(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _card(conn, 7)
        assert not replan.claim_refused(conn, tid)
        helpers._run(conn, tid)
        assert replan.claim_refused(conn, tid)
        assert kb.claim_task(conn, tid) is None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.claim_review_task(conn, tid) is None


def test_completed_only_reset_and_next_episode(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        parent = replan.hold(conn, tid)
        assert parent and replan.state(conn, tid).due
        assert kb.complete_task(conn, parent, result='New plan recorded')
        kb.recompute_ready(conn)
        assert replan.hold(conn, tid) is None
        assert not replan.state(conn, tid).due
        helpers._runs(conn, tid, 8)
        next_parent = replan.hold(conn, tid)
        assert next_parent and next_parent != parent
        assert kb.get_task(conn, next_parent).idempotency_key == f'replan:{tid}:{parent}'
        assert replan.state(conn, tid).count == 8


@pytest.mark.parametrize('failure', ['create', 'link', 'comment'])
def test_partial_creation_rolls_back(kanban_home, monkeypatch, failure):
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        symbol = {'create': 'create_task', 'link': 'link_tasks', 'comment': 'add_comment'}[failure]
        def fail(*a, **kw):
            raise ValueError('injected failure')
        monkeypatch.setattr(kb, symbol, fail)
        with pytest.raises(ValueError, match='injected'):
            replan.hold(conn, tid)
        assert kb.get_task(conn, tid).status == 'ready'
        assert kb.parent_ids(conn, tid) == []
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert not helpers._events(conn, tid, replan.REQUESTED)


def test_replan_exempt_and_three_failure_budget(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        parent = replan.hold(conn, tid)
        helpers._runs(conn, parent, 9)
        assert replan.state(conn, parent) is None
        assert replan.hold(conn, parent) is None
        for i in range(3):
            assert kb.claim_task(conn, parent)
            assert dispatch._record_task_failure(conn, parent, 'Worker crashed', outcome='crashed',
                                                 release_claim=True, end_run=True) == (i == 2)
            assert kb.get_task(conn, parent).status == ('triage' if i == 2 else 'ready')
        assert not kb.parent_ids(conn, parent)
        assert kb.get_task(conn, tid).status == 'todo'


def test_override_raises_threshold(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _card(conn, max_retries=12)
        assert not replan.state(conn, tid).due
        helpers._runs(conn, tid, 4)
        assert replan.state(conn, tid).due


def test_spend_hold_still_wins(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _card(conn, 15)
        spawns = helpers.Spawns()
        result = dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert (tid, 'spend_budget:triage') in result.no_progress_held
        assert kb.get_task(conn, tid).status == 'triage'
        assert not kb.parent_ids(conn, tid)


def test_concurrent_dispatchers_create_one_episode(kanban_home):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with kbc.connect_closing() as conn:
        tid = _card(conn)
    barrier = Barrier(2)
    def contender():
        with kbc.connect_closing() as conn:
            barrier.wait(timeout=10)
            return replan.hold(conn, tid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: contender(), range(2)))
    with kbc.connect_closing() as conn:
        parent, = kb.parent_ids(conn, tid)
        assert set(outcomes) <= {None, parent}
        assert len(helpers._events(conn, tid, replan.REQUESTED)) == 1
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2


def test_authenticated_release_grants_more_runs_without_losing_mandate(kanban_home):
    from hermes_cli import kanban_release as release
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        tid = _card(conn, 20)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, decision.EVENT, {'cause': 'scope_stopped', 'reason': 'scope ended'})
        result = release.release(conn, tid, runs=12, writer=release.human_writer('test-operator'))
        assert result['ok'], result
        mandate = release.current_mandate(conn, tid)
        assert mandate and mandate['runs'] == 12
        current = replan.state(conn, tid)
        assert current is not None and current.threshold == 32 and not current.due
        assert replan.hold(conn, tid) is None
        assert release.current_mandate(conn, tid) == mandate


def test_foreign_executor_refuses_but_does_not_create(kanban_home, monkeypatch):
    from hermes_cli import kanban_wake_check as wake
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        monkeypatch.setattr(wake, 'foreign_copy', lambda *a: True)
        assert replan.hold(conn, tid) is None
        assert replan.claim_refused(conn, tid)
        assert not kb.parent_ids(conn, tid)
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_claim_rechecks_counter_inside_transaction(kanban_home, monkeypatch):
    from contextlib import contextmanager
    with kbc.connect_closing() as conn:
        tid = _card(conn, 7)
        real = kb.claim_transaction
        @contextmanager
        def racing(*a, **kw):
            helpers._run(conn, tid)
            with real(*a, **kw) as admitted:
                yield admitted
        monkeypatch.setattr(kb, 'claim_transaction', racing)
        assert kb.claim_task(conn, tid) is None
        assert len(kb.list_runs(conn, tid)) == 8


@pytest.mark.parametrize('removed', ['archived', 'deleted', 'empty_plan'])
def test_cancelled_replan_is_replaced_without_reset(kanban_home, removed):
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        parent = replan.hold(conn, tid)
        assert parent
        if removed == 'archived':
            kb.archive_task(conn, parent)
        elif removed == 'deleted':
            kb.delete_task(conn, parent)
        else:
            # A malformed legacy/imported completion can lack a portable plan.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        kb.recompute_ready(conn)
        replacement = replan.hold(conn, tid)
        assert replacement and replacement != parent
        assert kb.get_task(conn, tid).status == 'todo'
        assert replan.claim_refused(conn, tid)
        current = replan.state(conn, tid)
        assert current is not None and current.count == 8
