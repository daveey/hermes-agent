"""No-agent orphan-stop backstop, with isolated boards and real CLI parsing."""
import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_stop_watch as watchdog
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home
NOW = 2_000_000_000


def stop(conn, *, age=1860, creator=None, cause=None):
    tid = kb.create_task(conn, title='Finish acceptance', assignee='default', created_by=creator)
    episode = next_stop(conn, tid, NOW - age, cause)
    return tid, episode


def next_stop(conn, tid, at, cause=None):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {'cause': cause})
        conn.execute("UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events WHERE task_id=?)",
                     (at, tid))
    return reviewer.episode_of(decision.pending_episode(conn, tid))


def filed(conn):
    return conn.execute("SELECT * FROM tasks WHERE created_by=?", (watchdog.AUTHOR,)).fetchall()


@pytest.mark.parametrize('cause', [None, 'scope_stopped'])
def test_files_one_with_context_and_repeats_silently(kanban_home, cause):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, cause=cause)
        assert [c['id'] for c in watchdog.stale_stops(conn, now=NOW)] == [tid]
        result = watchdog.watch(conn, now=NOW)
        assert len(result) == 1
        card = kb.get_task(conn, result[0])
        assert card.assignee == 'default'
        assert tid in card.body and 'Finish acceptance' in card.body
        assert (cause or 'run budget') in card.body
        assert 'UTC' in card.body and f'hermes kanban release {tid}' in card.body
        assert '31 min' in card.title
        assert card.idempotency_key == f'spend-stop-watch:{tid}:{ep}'
        assert watchdog.watch(conn, now=NOW) == []
        assert len(filed(conn)) == 1


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_watch_card_does_not_refile(kanban_home, status):
    with kbc.connect_closing() as conn:
        stop(conn)
        first, = watchdog.watch(conn, now=NOW)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, first))
        assert watchdog.watch(conn, now=NOW) == []
        assert len(filed(conn)) == 1


@pytest.mark.parametrize('age', [1740, 1800])
def test_not_strictly_older(kanban_home, age):
    with kbc.connect_closing() as conn:
        stop(conn, age=age)
        assert watchdog.watch(conn, now=NOW) == []


@pytest.mark.parametrize('creator', ['spend-stop-watch', 'kanban-spend-budget'])
def test_never_recurse(kanban_home, creator):
    with kbc.connect_closing() as conn:
        stop(conn, creator=creator)
        assert watchdog.watch(conn, now=NOW) == []


def test_safety_watch_only_escalates_without_release_advice(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = stop(conn, cause='safety_refusal')
        created, = watchdog.watch(conn, now=NOW)
        card = kb.get_task(conn, created)
        assert 'escalate to David' in card.body
        assert 'hermes kanban release' not in card.body
        assert kb.get_task(conn, tid).status == 'triage'
        assert watchdog.watch(conn, now=NOW) == []


@pytest.mark.parametrize('state', [reviewer.QUEUED, reviewer.STARTED, reviewer.GRANTED, reviewer.SETTLED])
def test_reviewed_episode(kanban_home, spend_reviewer, state):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, state, {'episode': ep})
        assert watchdog.watch(conn, now=NOW) == []


@pytest.mark.parametrize('state', [reviewer.QUEUED, reviewer.STARTED, reviewer.ESCALATED])
def test_abandoned_custody_does_not_hide_unrouted_stop(kanban_home, state):
    # The default fixture disables the reviewer: queued/started are not custody.
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, state, {'episode': ep})
        assert [c['id'] for c in watchdog.stale_stops(conn, now=NOW)] == [tid]


def test_real_decision_route(kanban_home):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        kb.create_task(conn, title='Decide stop', assignee='default', created_by='kanban-spend-budget',
                       idempotency_key=f'spend-budget-decision:{tid}:{ep}')
        assert decision.episode_route(conn, tid, ep) is not None
        assert watchdog.watch(conn, now=NOW) == []


def test_foreign_copy(kanban_home, monkeypatch):
    from hermes_cli import kanban_wake_check as wakes
    monkeypatch.setattr(wakes, 'own_gateway', lambda conn: 'home')
    with kbc.connect_closing() as conn:
        tid, _ = stop(conn)
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
            conn.execute("UPDATE tasks SET created_on='elsewhere' WHERE id=?", (tid,))
        assert tid in wakes.foreign_ids(conn)
        assert watchdog.watch(conn, now=NOW) == []


def test_new_episode_ignores_previous_review_route_and_marker(kanban_home):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        assert len(watchdog.watch(conn, now=NOW)) == 1
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.QUEUED, {'episode': ep})
        kb.create_task(conn, title='Previous decision', assignee='default', created_by='kanban-spend-budget',
                       idempotency_key=f'spend-budget-decision:{tid}:{ep}')
        newer = next_stop(conn, tid, NOW - 1850)
        assert newer != ep
        assert len(watchdog.watch(conn, now=NOW)) == 1
        assert len(filed(conn)) == 2


@pytest.mark.parametrize('condition', ['not_triage', 'no_stop', 'decided'])
def test_only_pending_triage_stops(kanban_home, condition):
    with kbc.connect_closing() as conn:
        tid, _ = stop(conn)
        with kb.write_txn(conn):
            if condition == 'not_triage':
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            elif condition == 'no_stop':
                conn.execute('DELETE FROM task_events WHERE task_id=? AND kind=?', (tid, decision.EVENT))
            else:
                kb._append_event(conn, tid, 'promoted_manual', {})
                conn.execute('UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events)', (NOW,))
        assert watchdog.watch(conn, now=NOW) == []


@pytest.mark.parametrize('error', [ValueError, PermissionError])
def test_refusal_rolls_back_and_can_retry(kanban_home, monkeypatch, error):
    with kbc.connect_closing() as conn:
        stop(conn)
        create = kb.create_task
        def refuse(*args, **kwargs):
            create(*args, **kwargs)
            raise error('admission refused')
        with monkeypatch.context() as scoped:
            scoped.setattr(kb, 'create_task', refuse)
            assert watchdog.watch(conn, now=NOW) == []
        assert filed(conn) == []
        assert len(watchdog.watch(conn, now=NOW)) == 1


def cli():
    from hermes_cli import kanban, kanban_parser
    parser = argparse.ArgumentParser()
    kanban_parser.build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(['kanban', 'spend-stop-watch']))


def test_cli_silent(kanban_home, capsys):
    assert cli() == 0
    assert capsys.readouterr() == ('', '')


def test_cli_reports_exact_receipt(kanban_home, capsys, monkeypatch):
    monkeypatch.setattr(watchdog.time, 'time', lambda: NOW)
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
    assert cli() == 0
    with kbc.connect_closing() as conn:
        card, = filed(conn)
    assert capsys.readouterr().out == f"filed {card['id']} for {tid} stop {ep}\n"


def _notice(conn, tid, ep, producer):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, decision.NOTICE, {'episode': ep, 'producer': producer})


@pytest.mark.parametrize('producer,files', [
    ({'kind': 'chats', 'sessions': ['s1']}, False), ({'kind': 'dismissed'}, False),
    ({'kind': 'unavailable'}, True), ('chats', True), (None, True)])
def test_notice_route_counts_only_when_somebody_was_told(kanban_home, producer, files):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        _notice(conn, tid, ep, producer)
        assert len(watchdog.watch(conn, now=NOW)) == (1 if files else 0)


def test_notice_route_of_another_episode_or_a_closed_decision_card_does_not_count(kanban_home):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        _notice(conn, tid, 'another-episode', {'kind': 'chats'})
        closed = kb.create_task(conn, title='Old decision desk', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (closed,))
        _notice(conn, tid, ep, {'kind': 'supervisor', 'task_id': closed})
        assert len(watchdog.watch(conn, now=NOW)) == 1


MALFORMED = [1, True, ['scope_stopped'], {'safety_refusal': True}]
MALFORMED_IDS = ['int', 'bool', 'list', 'dict']


def _malformed(conn, cause):
    """A triage stop whose cause is not a string: the validator refuses to interpret it."""
    tid = kb.create_task(conn, title='Malformed stop', assignee='default')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {'cause': cause})
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (NOW - 7200, tid))
    with pytest.raises(ValueError, match='cause'):
        decision.pending_episode(conn, tid)
    return tid


@pytest.mark.parametrize('cause', MALFORMED, ids=MALFORMED_IDS)
def test_malformed_cause_is_isolated_and_healthy_neighbor_is_filed(kanban_home, caplog, cause):
    with kbc.connect_closing() as conn:
        bad = _malformed(conn, cause)
        good, ep = stop(conn)
        assert [c['id'] for c in watchdog.stale_stops(conn, now=NOW)] == [good]
        new_id, = watchdog.watch(conn, now=NOW)
        assert kb.get_task(conn, new_id).idempotency_key == f'spend-stop-watch:{good}:{ep}'
        assert kb.get_task(conn, bad).status == 'triage'  # the malformed hold is retained, never released
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                                (bad, watchdog.FILED)).fetchone()
        assert bad in caplog.text
        assert not conn.in_transaction
