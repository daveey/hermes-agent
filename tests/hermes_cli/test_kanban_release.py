"""``hermes kanban release``: one step from a spend/scope stop to a card that can finish. [daveey]"""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board
HUMAN = release.human_writer('test-operator')


def spend_stopped(board):
    """An ordinary spend stop: triage under a pending decision, no scope restriction."""
    tid = kb.create_task(board, title='Retained work', assignee='default')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: 15 runs'})
    return tid


def run(board, tid, **kw):
    return release.release(board, tid, writer=kw.pop('writer', HUMAN), **kw)


def status(board, tid):
    return kb.get_task(board, tid).status


def written(board, tid):
    """Everything a release writes on a card: comments and events, as counts."""
    return (board.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (tid,)).fetchone()[0],
            board.execute('SELECT COUNT(*) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0])


def spent_runs(board, tid, n):
    """``n`` closed spending runs inside the card's current window."""
    start = budget._window_start(board, tid, 2999999999) + 10
    with kb.write_txn(board):
        for i in range(n):
            board.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) VALUES (?,?,?,?,?)",
                          (tid, 'blocked', start + i * 200, start + i * 200 + 120, 'blocked'))


def test_scope_stop_released_by_a_human(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    out = run(board, tid, runs=7)
    assert out['outcome'] == 'released' and out['ok'] and out['scope_renewed'] and out['episode'] == episode
    assert status(board, tid) == 'ready'
    assert not decision.pending(board, tid) and not decision.execution_restricted(board, tid)
    body = board.execute('SELECT body FROM task_comments WHERE id=?', (out['mandate_comment'],)).fetchone()[0]
    assert body == release.MANDATE.format(runs=7) and 'Budget: 7 more runs' in body
    renewed = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scope_renewed'",
                                       (tid,)).fetchone()[0])
    assert renewed['authority_kind'] == 'human' and renewed['mandate_comment_id'] == out['mandate_comment']
    assert board.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE ?",
                         (tid, release.MARK + '%')).fetchone()[0] == 1
    assert release.MARK in kb.build_worker_context(board, tid)  # the next run reads the goal-level mandate


def test_ordinary_spend_stop_released(board):
    tid = spend_stopped(board)
    out = run(board, tid, note='Desktop Unblock, operation op-1')
    assert out['outcome'] == 'released' and out['scope_renewed'] is False and out['runs'] == release.DEFAULT_RUNS
    assert status(board, tid) == 'ready' and not decision.pending(board, tid)
    body = board.execute('SELECT body FROM task_comments WHERE id=?', (out['mandate_comment'],)).fetchone()[0]
    assert body.endswith('\n\nDesktop Unblock, operation op-1')
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(release.DEFAULT_RUNS, 0)
    assert budget.hold(board, tid) is None  # the dispatcher does not send it straight back to triage


def test_decision_card_id_resolves_to_its_root_and_is_archived(board):
    tid = spend_stopped(board)
    decision.ensure_producer(board, tid)
    card = board.execute("SELECT id FROM tasks WHERE created_by='kanban-spend-budget'").fetchone()[0]
    assert release.target(board, card) == tid and release.held(board, card) == tid
    filed = board.execute('SELECT body FROM tasks WHERE id=?', (card,)).fetchone()[0]
    assert release.ONE_STEP.format(task=tid) in filed  # the decision card names the one-step route for its root
    out = run(board, card)
    assert out['outcome'] == 'released' and out['task_id'] == card and out['target'] == tid
    assert status(board, tid) == 'ready'
    assert out['archived_decisions'] == [card] and status(board, card) == 'archived'
    note = board.execute('SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1', (card,)).fetchone()[0]
    assert f'hermes kanban release {tid}' in note


def test_open_parent_waits_in_todo_and_starts_when_the_parent_finishes(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    parent = kb.create_task(board, title='parent', assignee='default')
    kb.link_tasks(board, parent, tid)
    out = run(board, tid)
    assert out['outcome'] == 'waiting_on_parents' and out['parents'] == [parent] and out['scope_renewed']
    assert status(board, tid) == 'todo'
    assert board.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                         (tid,)).fetchone()[0] == 'dependency_wait'
    assert budget.card_limits(board, tid, budget.limits()).runs == out['budget_runs']
    # The decision is recorded: when the parent finishes the card becomes ready and is not held again.
    assert not decision.pending(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    kb.recompute_ready(board)
    assert status(board, tid) == 'ready' and budget.hold(board, tid) is None


def test_safety_restriction_is_refused_with_nothing_written(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    before = written(board, tid)
    out = run(board, tid)
    assert out['outcome'] == 'refused' and out['refusal'] == 'safety_restricted' and not out['ok']
    assert 'safety' in out['message'].lower()
    assert written(board, tid) == before and status(board, tid) == 'triage'


def test_containment_hold_is_refused_with_nothing_written(board, monkeypatch):
    tid = spend_stopped(board)
    monkeypatch.setattr('hermes_cli.kanban_containment_guard.pending', lambda conn, task, **kw: True)
    before = written(board, tid)
    out = run(board, tid)
    assert out['refusal'] == 'containment_hold' and written(board, tid) == before


def test_a_run_of_the_stopped_card_is_refused(board, monkeypatch):
    tid, session, _ = scope.stopped(board, monkeypatch)
    before = written(board, tid)
    own = ('agent', session, '9:HERMES_SESSION_ID')
    out = release.release(board, tid, writer=own, env={'HERMES_SESSION_ID': session},
                          ancestry=[{'HERMES_SESSION_ID': session}])
    assert out['outcome'] == 'refused' and out['refusal'] == 'own_run'
    marked = release.release(board, tid, writer=scope.AGENT, env={'HERMES_KANBAN_TASK': tid},
                             ancestry=[{'HERMES_SESSION_ID': scope.CHAT}])
    assert marked['refusal'] == 'own_run'
    assert written(board, tid) == before and status(board, tid) == 'triage'


def test_owning_chat_releases_and_an_unrelated_agent_is_refused(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    before = written(board, tid)
    other = ('agent', 'another-session', '1:HERMES_SESSION_ID')
    out = release.release(board, tid, writer=other, env={'HERMES_SESSION_ID': 'another-session'},
                          ancestry=[{'HERMES_SESSION_ID': 'another-session'}])
    assert out['refusal'] == 'not_scope_authority' and written(board, tid) == before
    started = {'HERMES_SESSION_ID': scope.CHAT}
    out = release.release(board, tid, writer=scope.AGENT, env=started, ancestry=[started])
    assert out['outcome'] == 'released', out
    renewed = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scope_renewed'",
                                       (tid,)).fetchone()[0])
    assert renewed['authority_kind'] == 'chat'


def test_rerun_is_already_released_and_writes_nothing(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    assert run(board, tid)['outcome'] == 'released'
    before = written(board, tid)
    again = run(board, tid)
    assert again['outcome'] == 'already_released' and again['ok'] and written(board, tid) == before


def test_a_card_that_is_not_held_is_refused(board):
    tid = kb.create_task(board, title='plain', assignee='default')
    before = written(board, tid)
    for state in ('ready', 'running', 'done', 'archived', 'triage'):
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status=? WHERE id=?", (state, tid))
        out = run(board, tid)
        assert out['outcome'] == 'refused' and out['refusal'] == 'not_held', state
    assert written(board, tid) == before
    assert run(board, 't_missing')['refusal'] == 'no_such_task'


def test_budget_is_runs_already_counted_plus_the_new_runs(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    spent_runs(board, tid, 4)
    spent = budget.spend(board, tid).runs
    assert spent >= 4
    out = run(board, tid, runs=6)
    assert out['spent_runs'] == spent and out['budget_runs'] == spent + 6
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(spent + 6, 0)
    assert budget.hold(board, tid) is None and status(board, tid) == 'ready'


def argv(*args):
    import argparse
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(['kanban', 'release', *args]))


def test_command_line_bulk_releases_the_others_when_one_is_refused(board, monkeypatch, capsys):
    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: HUMAN)
    first, second = spend_stopped(board), spend_stopped(board)
    plain = kb.create_task(board, title='plain', assignee='default')
    assert argv(first, '--ids', plain, second, '--runs', '3', '--note', 'on his word', '--json') == 1
    results = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [r['outcome'] for r in results] == ['released', 'refused', 'released']
    assert results[1]['refusal'] == 'not_held' and results[0]['budget_runs'] == 3
    assert status(board, first) == status(board, second) == 'ready'
    assert argv(first) == 0  # text mode, idempotent rerun
    assert f'{first}: already_released' in capsys.readouterr().out
    assert argv(plain) == 1
    assert f'{plain}: refused: not_held' in capsys.readouterr().err


def test_default_runs_come_from_config():
    assert release.default_runs({}) == 10
    assert release.default_runs({'kanban': {'release_runs': 25}}) == 25
    assert release.default_runs({'kanban': {'release_runs': 0}}) == 10


def test_stop_notices_name_the_one_step_route_and_a_goal_level_mandate(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    plain = spend_stopped(board)
    one_step = release.ONE_STEP.format(task=tid)
    assert f'`hermes kanban release {tid}`' in one_step and 'goal-level mandate' in one_step
    assert one_step in decision.notice({'cause': 'scope_stopped', 'task_id': tid})
    assert one_step in decision.execution_refusal(board, tid)
    assert one_step in decision.unblock_refusal(board, tid)
    assert release.ONE_STEP.format(task=plain) in decision.notice({'reason': 'Spend budget', 'task_id': plain})
    assert release.ONE_STEP.format(task=plain) in decision.unblock_refusal(board, plain)
    assert 'kanban release' not in decision.notice({'cause': 'safety_refusal', 'task_id': tid})


def test_brief_on_a_decision_card_says_unblock_releases_the_stopped_card(board):
    from hermes_cli import kanban_stranded_brief as brief
    tid = spend_stopped(board)
    decision.ensure_producer(board, tid)
    card = dict(board.execute("SELECT * FROM tasks WHERE created_by='kanban-spend-budget'").fetchone())
    request = brief.request(board, card, None, 3 * 3600, 'events: spend_budget_triaged runs=15')
    assert (f'Unblock = release it to finish with 10 more runs under a goal-level scope '
            f'(`hermes kanban release {tid}`); safety holds still apply') in request['action']
    assert 'A) Unblock: Release the stopped work to finish its acceptance: 10 more runs' in request['scope']
    # A decision card whose stopped card is no longer held keeps the plain "return to an agent" brief.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    plain = brief.request(board, card, None, 3 * 3600, 'events: spend_budget_triaged runs=15')
    assert 'kanban release' not in plain['action'] and 'return this decision to an agent' in plain['action']
