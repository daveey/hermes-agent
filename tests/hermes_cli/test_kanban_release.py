"""``hermes kanban release``: one step from a spend/scope stop to a card that can finish. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board
HUMAN = release.human_writer('test-operator')


def filed(board, card, request):
    """What the stranded-park sweep records when it files ``request`` on ``card`` (``_escalate``)."""
    kb._append_event(board, card, 'stranded_park_escalated', {'request': True,
                                                              'release_target': request['release_target']})
    board.commit()


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
    """Everything a release writes on a card: comments and events, as counts. A read-first refusal's own
    ``release_unread_shown`` receipt (what it printed to which session) is not a write of the release."""
    return (board.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (tid,)).fetchone()[0],
            board.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind != ?',
                          (tid, release.SHOWN)).fetchone()[0])


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
    assert body == release.mandate_text(7) and 'Budget: 7 more runs' in body
    assert 'conditions that agents wrote earlier are superseded.' in body and 'David set stays' not in body
    record = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='stop_released'",
                                      (tid,)).fetchone()[0])
    assert record == {'version': 1, 'episode': episode, 'authority_kind': 'human', 'actor_kind': 'human',
                      'actor_session': None, 'runs': 7, 'budget_runs': out['budget_runs'],
                      'mandate_comment_id': out['mandate_comment'], 'mandate_comment_uid': record['mandate_comment_uid'],
                      'note': None}
    renewed = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scope_renewed'",
                                       (tid,)).fetchone()[0])
    assert renewed['authority_kind'] == 'human' and renewed['mandate_comment_id'] == out['mandate_comment']
    assert board.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE ?",
                         (tid, release.MARK + '%')).fetchone()[0] == 1
    assert release.MARK in kb.build_worker_context(board, tid)  # the next run reads the goal-level mandate


def _legacy_decision(conn, tid):
    """Replay existing administrative wrappers; publication must never create one."""
    from tests.hermes_cli.test_kanban_spend_route_repair import _historical_wrapper
    card = _historical_wrapper(conn, tid)
    body = decision.notice({'task_id': tid, 'for_worker': True})
    body += f"\n`hermes kanban promote {tid} --reason '<what changed>'`, or archive it"
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET body=? WHERE id=?', (body, card))
    return card


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
    card = _legacy_decision(board, tid)
    assert release.target(board, card) == tid and release.held(board, card) == tid
    filed = board.execute('SELECT body FROM tasks WHERE id=?', (card,)).fetchone()[0]
    # Its worker is fenced from the verb: the body says who can release, never "run release <root>" to the worker.
    assert release.WORKER_NOTE.format(task=tid) in filed and f'hermes kanban release {tid}' not in filed
    assert f"`hermes kanban promote {tid} --reason" in filed and 'or archive it' in filed
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
    authority = {'chats': [{'gateway': 'own', 'profile': 'default', 'session': 'creator'}]}
    assert one_step in decision.notice({'cause': 'scope_stopped', 'task_id': tid, 'authority': authority})
    assert 'No recorded renewable agent authority' in decision.notice({'cause': 'scope_stopped', 'task_id': tid})
    assert one_step in decision.execution_refusal(board, tid)
    assert one_step in decision.unblock_refusal(board, tid)
    assert release.ONE_STEP.format(task=plain) in decision.notice({'reason': 'Spend budget', 'task_id': plain})
    assert release.ONE_STEP.format(task=plain) in decision.unblock_refusal(board, plain)
    assert 'kanban release' not in decision.notice({'cause': 'safety_refusal', 'task_id': tid})


def test_brief_on_a_decision_card_says_unblock_releases_the_stopped_card(board):
    from hermes_cli import kanban_stranded_brief as brief
    tid = spend_stopped(board)
    cid = _legacy_decision(board, tid)
    card = dict(board.execute("SELECT * FROM tasks WHERE id=?", (cid,)).fetchone())
    request = brief.request(board, card, None, 3 * 3600, 'events: spend_budget_triaged runs=15')
    assert (f'Unblock = release it to finish with 10 more runs under a goal-level scope '
            f'(`hermes kanban release {tid}`); safety holds still apply') in request['action']
    episode = scope.renewal.key(decision.pending_episode(board, tid))
    assert request['release_target'] == {'task': tid, 'episode': episode}  # the offer the click checks
    assert release.offered(board, card['id'], request) is None  # a stamp alone, with no record of the home filing it
    filed(board, card['id'], request)
    assert release.offered(board, card['id'], request) == tid
    assert 'A) Unblock: Release the stopped work to finish its acceptance: 10 more runs' in request['scope']
    assert 'Recommend: A - release it to finish' in request['scope']
    # A decision card whose stopped card is no longer held keeps the plain "return to an agent" brief.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    plain = brief.request(board, card, None, 3 * 3600, 'events: spend_budget_triaged runs=15')
    assert 'kanban release' not in plain['action'] and 'return this decision to an agent' in plain['action']


def test_brief_on_a_safety_restricted_card_keeps_the_cautious_line(board, monkeypatch):
    from hermes_cli import kanban_stranded_brief as brief
    tid, _, episode = scope.stopped(board, monkeypatch)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    row = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    request = brief.request(board, row, None, 3 * 3600, 'events: decision_hold_restricted')
    assert 'Unblock = no release: a provider safety hold stays' in request['action']
    assert 'release_target' not in request and release.offer(board, tid) is None
    assert 'Recommend: B - avoid authorizing more work without a bounded proposal.' in request['scope']
    assert 'release it to finish' not in request['action'] + request['scope']


def test_released_by_names_the_card_only_for_the_note_that_released_it(board):
    tid = spend_stopped(board)
    note = release.desktop_note('op-7')
    assert note == 'Desktop Unblock, operation op-7' and release.released_by(board, tid, note) is None
    run(board, tid, note=note)
    assert release.released_by(board, tid, note) == tid
    assert release.released_by(board, tid, release.desktop_note('op-8')) is None
    assert release.released_by(board, tid, '') is None


TRIAGE = '20261123_101500_a1b2c3'  # a desk chat: no link to the stopped card, no run of it


def as_triage(board, tid, **kw):
    started = {'HERMES_SESSION_ID': TRIAGE}
    return release.release(board, tid, writer=('agent', TRIAGE, '4242:HERMES_SESSION_ID'), env=started,
                           ancestry=[started], **kw)


def test_a_delegate_chat_releases_a_scope_stopped_card_it_does_not_own(board, monkeypatch):
    from hermes_cli import kanban_scope_delegate as delegates
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)  # another chat owns the card
    before = written(board, tid)
    out = as_triage(board, tid)  # a non-owner chat without a grant is still refused, with nothing written
    assert out['outcome'] == 'refused' and out['refusal'] == 'not_scope_authority'
    assert written(board, tid) == before and status(board, tid) == 'triage'
    made = delegates.grant(board, session=TRIAGE, reason='triage chat may unblock cards', writer=scope.HUMAN)
    out = as_triage(board, tid, runs=5)
    assert out['outcome'] == 'released', out
    assert status(board, tid) == 'ready' and not decision.pending(board, tid)
    renewed = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scope_renewed'",
                                       (tid,)).fetchone()[0])
    assert renewed['authority_kind'] == 'delegate' and renewed['authority_ref'] == made['uid']
    assert renewed['actor_session'] == TRIAGE


def test_a_delegate_chat_releases_an_ordinary_spend_stop_and_a_revoked_grant_is_refused(board):
    from hermes_cli import kanban_scope_delegate as delegates
    first, second = spend_stopped(board), spend_stopped(board)
    delegates.grant(board, session=TRIAGE, reason='triage chat may unblock cards', writer=scope.HUMAN)
    assert as_triage(board, first)['outcome'] == 'released'
    delegates.revoke(board, session=TRIAGE, reason='done', writer=scope.HUMAN)
    before = written(board, second)
    out = as_triage(board, second)
    assert out['outcome'] == 'refused' and out['refusal'] == 'not_scope_authority'
    assert written(board, second) == before and status(board, second) == 'triage'


# ---- review round: validate first, read before acting, reruns, the durable record, the release offer ----

OWNER = {'HERMES_SESSION_ID': scope.CHAT}


def as_owner(board, tid, **kw):
    return release.release(board, tid, writer=scope.AGENT, env=OWNER, ancestry=[OWNER], **kw)


def mandates(board, tid):
    return board.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE ?",
                         (tid, release.MARK + '%')).fetchone()[0]


def records(board, tid):
    return board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stop_released'",
                         (tid,)).fetchone()[0]


def test_invalid_runs_is_refused_with_nothing_written(board):
    tid = spend_stopped(board)
    before = written(board, tid)
    for bad in (0, -3, True, '5'):
        assert run(board, tid, runs=bad)['refusal'] == 'invalid_runs'
    assert written(board, tid) == before


def test_fenced_shell_cannot_run_release(board, monkeypatch, capsys):
    from hermes_cli import kanban
    tid = spend_stopped(board)
    assert 'release' in kanban._DELEGATED_CHILD_DENIED_ACTIONS
    monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced', lambda path: True)
    before = written(board, tid)
    assert argv(tid) != 0
    assert 'cannot mutate Kanban tasks' in capsys.readouterr().err
    assert written(board, tid) == before and status(board, tid) == 'triage'


def test_scope_restriction_outside_triage_is_not_stopped(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    before = written(board, tid)
    assert run(board, tid)['refusal'] == 'not_stopped' and written(board, tid) == before


def test_scheduled_card_with_an_open_parent_and_a_missing_parent_are_refused_up_front(board):
    tid, gone = spend_stopped(board), spend_stopped(board)
    parent = kb.create_task(board, title='parent', assignee='default')
    kb.link_tasks(board, parent, tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        board.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_gone', ?)", (gone,))
    before = written(board, tid), written(board, gone)
    assert run(board, tid)['refusal'] == 'scheduled_open_parent'
    assert run(board, gone)['refusal'] == 'parent_unresolved'
    assert (written(board, tid), written(board, gone)) == before


def test_agent_authority_must_have_read_a_human_comment_written_after_the_stop(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Hold this until Friday.', writer_kind='human')
    before = written(board, tid)
    out = as_owner(board, tid)
    assert out['refusal'] == 'budget_decision_unread' and out['unread'] == [said]
    assert f'Comment {said} | default (human) | ' in out['message'] and 'Hold this until Friday.' in out['message']
    assert f'--seen {said}' in out['message']
    # --seen is the id the refusal showed: not a larger number, not a guess.
    for wrong in (said + 1, 10 ** 9, said - 1):
        again = as_owner(board, tid, seen=wrong)
        assert again['refusal'] == 'budget_decision_unread' and f'--seen {wrong} is not the newest' in again['message']
    assert written(board, tid) == before
    assert written(board, tid) == before and status(board, tid) == 'triage'
    assert as_owner(board, tid, seen=said)['outcome'] == 'released'


def test_agent_authority_must_have_read_a_budget_decision_and_a_human_proceeds(board, monkeypatch):
    agent_card, human_card = spend_stopped(board), spend_stopped(board)
    for tid in (agent_card, human_card):
        scope.link(board, tid)
        budget.set_override(board, tid, runs=3, by='david', reason='three more, then stop')
    before = written(board, agent_card)
    out = as_owner(board, agent_card)
    assert out['refusal'] == 'budget_decision_unread' and out['unread'] and written(board, agent_card) == before
    assert f"--seen {max(out['unread'])}" in out['message']
    assert as_owner(board, agent_card, seen=max(out['unread']))['outcome'] == 'released'
    assert run(board, human_card)['outcome'] == 'released'  # a human caller is not asked to prove a read


def test_an_exempt_card_stays_exempt(board):
    tid = spend_stopped(board)
    budget.set_override(board, tid, runs=0, cache_read_tokens=0, by='david', reason='standing card')
    out = run(board, tid, runs=4)
    assert out['outcome'] == 'released' and out['budget_runs'] == 0
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(0, 0)
    assert json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spend_budget_set' "
                                    "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])['by'] == 'david'


def test_agent_mandate_supersedes_only_what_agents_wrote(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    out = as_owner(board, tid)
    body = board.execute('SELECT body FROM task_comments WHERE id=?', (out['mandate_comment'],)).fetchone()[0]
    assert 'written by agents earlier are superseded.' in body and 'David set' not in body
    total = out['budget_runs']
    assert (f"The card's budget after this release: {total} runs (10 more than the {out['spent_runs']} counted), "
            'no cache-read limit.') in body
    assert body == release.mandate_text(10, agent=True, budget=f"{total} runs (10 more than the "
                                        f"{out['spent_runs']} counted), no cache-read limit")
    record = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='stop_released'",
                                      (tid,)).fetchone()[0])
    assert record['authority_kind'] == 'chat' and record['actor_session'] == scope.CHAT


def test_rerun_after_board_unavailable_reuses_the_mandate_comment(board, monkeypatch):
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    tid, _, _ = scope.stopped(board, monkeypatch)
    real = renewal_cli.renew

    def down(*a, **kw):
        raise scope.RenewalRefused('board_unavailable', 'the Board could not be reached')
    monkeypatch.setattr(renewal_cli, 'renew', down)
    out = run(board, tid, note='on his word')
    assert out['refusal'] == 'board_unavailable' and 'a rerun reuses it' in out['message']
    assert mandates(board, tid) == 1 and records(board, tid) == 0 and status(board, tid) == 'triage'
    monkeypatch.setattr(renewal_cli, 'renew', real)
    again = run(board, tid, note='on his word')
    assert again['outcome'] == 'released' and again['mandate_reused'] is True
    assert again['mandate_comment'] == out['mandate_comment'] and mandates(board, tid) == 1
    # A different caller or a different text is not "the same mandate": nothing of someone else's is adopted.
    other = spend_stopped(board)
    kb.add_comment(board, other, 'default', release.mandate_text(release.DEFAULT_RUNS), writer_kind='agent',
                   writer_session='someone-else')
    assert run(board, other)['mandate_reused'] is False and mandates(board, other) == 2


def test_promote_refused_records_nothing_and_a_rerun_finishes(board, monkeypatch):
    tid = spend_stopped(board)
    real = kb.promote_task
    monkeypatch.setattr(kb, 'promote_task', lambda *a, **kw: (False, 'promotion is down'))
    out = run(board, tid)
    assert out['refusal'] == 'promote_refused' and 'promotion is down' in out['message']
    assert status(board, tid) == 'triage' and decision.pending(board, tid) and records(board, tid) == 0
    monkeypatch.setattr(kb, 'promote_task', real)
    again = run(board, tid)
    assert again['outcome'] == 'released' and again['mandate_reused'] and mandates(board, tid) == 1
    assert records(board, tid) == 1 and status(board, tid) == 'ready'


def test_dependency_wait_refused_is_finished_by_a_rerun(board, monkeypatch):
    tid = spend_stopped(board)
    parent = kb.create_task(board, title='parent', assignee='default')
    kb.link_tasks(board, parent, tid)
    real = kb.block_task
    monkeypatch.setattr(kb, 'block_task', lambda *a, **kw: False)
    out = run(board, tid)
    assert out['refusal'] == 'dependency_wait_refused' and 'rerun to finish' in out['message']
    assert status(board, tid) == 'triage' and not decision.pending(board, tid) and records(board, tid) == 1
    monkeypatch.setattr(kb, 'block_task', real)
    again = run(board, tid)
    assert again['outcome'] == 'waiting_on_parents' and again['resumed'] and status(board, tid) == 'todo'
    assert mandates(board, tid) == 1 and records(board, tid) == 1
    assert run(board, tid)['outcome'] == 'already_released'
    # The same partial state with the parent finished meanwhile is finished by a promotion.
    late = spend_stopped(board)
    kb.link_tasks(board, parent, late)
    monkeypatch.setattr(kb, 'block_task', lambda *a, **kw: False)
    assert run(board, late)['refusal'] == 'dependency_wait_refused'
    monkeypatch.setattr(kb, 'block_task', real)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    done = run(board, late)
    assert done['outcome'] == 'released' and done['resumed'] and status(board, late) == 'ready'


def test_already_released_cannot_be_faked_by_a_comment(board):
    tid = spend_stopped(board)
    kb.add_comment(board, tid, 'default', release.mandate_text(10), writer_kind='agent', writer_session='x')
    assert kb.promote_task(board, tid, actor='someone', reason='plain promote')[0]
    out = run(board, tid)
    assert out['outcome'] == 'refused' and out['refusal'] == 'not_held'
    assert release.released_by(board, tid, 'anything') is None


def test_a_precondition_is_asked_before_writing_and_inside_the_deciding_transaction(board):
    tid = spend_stopped(board)
    before = written(board, tid)
    assert run(board, tid, precondition=lambda c: 'changed ask')['refusal'] == 'ask_changed'
    assert written(board, tid) == before
    calls = []

    def late(c):
        calls.append(c.in_transaction)
        return 'changed ask' if len(calls) > 1 else None
    out = run(board, tid, precondition=late)
    assert out['refusal'] == 'ask_changed' and calls == [False, True]
    assert status(board, tid) == 'triage' and decision.pending(board, tid) and records(board, tid) == 0
    assert run(board, tid)['outcome'] == 'released' and mandates(board, tid) == 1


def test_only_the_released_stops_decision_cards_are_archived(board):
    tid = spend_stopped(board)
    mine = _legacy_decision(board, tid)
    earlier = kb.create_task(board, title='Spend budget decision: earlier stop', assignee='default',
                             created_by='kanban-spend-budget')
    elsewhere = kb.create_task(board, title='Spend budget decision: another card', assignee='default',
                               created_by='kanban-spend-budget')
    kb._append_event(board, earlier, 'administrative_decision', {'root_task': tid, 'root_episode': 'an-earlier-stop'})
    kb._append_event(board, elsewhere, 'administrative_decision', {'root_task': 't_other', 'root_episode': 'x'})
    board.commit()
    out = run(board, tid)
    assert out['archived_decisions'] == [mine] and status(board, mine) == 'archived'
    assert status(board, earlier) != 'archived' and status(board, elsewhere) != 'archived'
    # A decision card for an earlier stop offers nothing, and a forged stamp for it is not an offer either.
    again = spend_stopped(board)
    stale = kb.create_task(board, title='Spend budget decision: stale', assignee='default',
                           created_by='kanban-spend-budget')
    kb._append_event(board, stale, 'administrative_decision', {'root_task': again, 'root_episode': 'stop-1'})
    board.commit()
    assert release.held(board, stale) == again and release.offer(board, stale) is None
    assert release.offered(board, stale, {'release_target': {'task': again, 'episode': 'stop-1'}}) is None


def test_no_supplied_request_can_carry_the_release_offer(board):
    from hermes_cli.kanban_human_request import validate_human_request
    tid = spend_stopped(board)
    offer = release.offer(board, tid)
    assert offer == {'task': tid, 'episode': scope.renewal.key(decision.pending_episode(board, tid))}
    forged = {'action': 'Unblock = I archive the root.', 'why_human': 'Only you can decide.', 'interaction': 'approval',
              'scope': 'An agent wrote this ask.', 'evidence': 'none', 'release_target': offer}
    assert 'release_target' not in validate_human_request(forged)
    card = kb.create_task(board, title='agent ask', assignee='default')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (card,))
    assert kb.block_task(board, card, kind='needs_input', reason='ask', human_request=forged)
    stored = json.loads(board.execute('SELECT human_request FROM tasks WHERE id=?', (card,)).fetchone()[0])
    assert 'release_target' not in stored
    # The offer is honoured only while it is still true, and only when this board's sweep recorded filing it.
    assert release.offered(board, tid, {'release_target': offer}) is None
    filed(board, tid, {'release_target': offer})
    assert release.offered(board, tid, {'release_target': offer}) == tid
    assert release.offered(board, tid, {}) is None and release.offered(board, tid, None) is None
    assert release.offered(board, tid, {'release_target': {**offer, 'episode': 'another-stop'}}) is None
    assert release.offered(board, tid, {'release_target': {**offer, 'task': 't_other'}}) is None
    run(board, tid)
    assert release.offered(board, tid, {'release_target': offer}) is None  # nothing is held any more


# ---- follow-up: resume, --seen, budget shape, stale mandates and decision cards, the home's own record ----

def test_a_released_card_put_back_in_triage_for_another_reason_is_not_promoted(board):
    tid = spend_stopped(board)
    scope.link(board, tid)
    assert run(board, tid)['outcome'] == 'released'
    with kb.write_txn(board):  # a block loop parked it again, with another agent's reason
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(board, tid, 'block_loop_detected', {'kind': 'transient', 'reason': 'same failure 3 times'})
    kb.add_comment(board, tid, 'default', 'Do not restart: the migration is half applied.', writer_kind='agent',
                   writer_session='another-agent')
    before = written(board, tid)
    for caller in (run, as_owner):
        out = caller(board, tid)
        assert out['outcome'] == 'refused' and out['refusal'] == 'not_held', out
    assert status(board, tid) == 'triage' and written(board, tid) == before


def test_finishing_a_recorded_release_still_reads_first(board, monkeypatch):
    tid = spend_stopped(board)
    scope.link(board, tid)
    parent = kb.create_task(board, title='parent', assignee='default')
    kb.link_tasks(board, parent, tid)
    real = kb.block_task
    monkeypatch.setattr(kb, 'block_task', lambda *a, **kw: False)
    assert as_owner(board, tid)['refusal'] == 'dependency_wait_refused'
    monkeypatch.setattr(kb, 'block_task', real)
    note = kb.add_comment(board, tid, 'default', 'Do not restart yet.', writer_kind='agent',
                          writer_session='another-agent')
    out = as_owner(board, tid)
    assert out['refusal'] == 'budget_decision_unread' and 'Do not restart yet.' in out['message']
    assert status(board, tid) == 'triage'
    newest = max(out['unread'])
    assert newest >= note and as_owner(board, tid, seen=newest)['outcome'] == 'waiting_on_parents'


def test_seen_without_anything_to_read_is_refused(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    before = written(board, tid)
    out = as_owner(board, tid, seen=10 ** 6)
    assert out['refusal'] == 'seen_not_needed' and written(board, tid) == before
    assert as_owner(board, tid)['outcome'] == 'released'


def test_an_agent_keeps_the_shape_of_a_limit_it_cannot_attribute_to_an_agent(board):
    kept, agents, reached = spend_stopped(board), spend_stopped(board), spend_stopped(board)
    for tid in (kept, agents, reached):
        scope.link(board, tid)
    budget.set_override(board, kept, runs=3, cache_read_tokens=5_000_000, by='david', reason='small and cheap')
    budget.set_override(board, agents, runs=3, cache_read_tokens=5_000_000, by='bot', reason='x', writer_kind='agent')
    assert json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spend_budget_set'",
                                    (agents,)).fetchone()[0])['writer_kind'] == 'agent'

    def release_as_owner(tid):
        shown = as_owner(board, tid)
        assert shown['refusal'] == 'budget_decision_unread'
        return as_owner(board, tid, seen=max(shown['unread']))

    out = release_as_owner(kept)
    assert out['outcome'] == 'released' and out['budget_runs'] == out['spent_runs'] + 10
    assert budget.card_limits(board, kept, budget.limits()) == budget.Limits(out['budget_runs'], 5_000_000)
    body = board.execute('SELECT body FROM task_comments WHERE id=?', (out['mandate_comment'],)).fetchone()[0]
    assert 'cache-read limit kept at 5000000.' in body and 'David set' not in body
    newest = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spend_budget_set' "
                                      "ORDER BY id DESC LIMIT 1", (kept,)).fetchone()[0])
    assert newest['writer_kind'] == 'agent' and newest['cache_read_tokens'] == 5_000_000
    # A limit an agent set is an agent's to replace; a human caller lifts the cache-read limit as before.
    out = release_as_owner(agents)
    assert budget.card_limits(board, agents, budget.limits()) == budget.Limits(out['budget_runs'], 0)
    human = spend_stopped(board)
    budget.set_override(board, human, runs=3, cache_read_tokens=5_000_000, by='david', reason='small')
    assert run(board, human)['outcome'] == 'released'
    assert budget.card_limits(board, human, budget.limits()).cache_read_tokens == 0


def test_an_agent_is_refused_when_the_kept_limit_is_already_reached(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    budget.set_override(board, tid, runs=50, cache_read_tokens=1000, by='david', reason='cheap only')
    spent = budget.spend(board, tid)
    monkeypatch.setattr(budget, 'spend', lambda conn, task, **kw: spent._replace(
        tokens={**spent.tokens, 'cache_read_tokens': 5000}))
    shown = as_owner(board, tid)
    before = written(board, tid)
    out = as_owner(board, tid, seen=max(shown['unread']))
    assert out['refusal'] == 'budget_limit_in_force' and '5000 of 1000' in out['message']
    assert written(board, tid) == before and status(board, tid) == 'triage'
    assert run(board, tid)['outcome'] == 'released'  # a human raises it


def test_a_mandate_older_than_new_scope_evidence_is_not_reused(board, monkeypatch):
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    tid, _, episode = scope.stopped(board, monkeypatch)
    real = renewal_cli.renew

    def down(*a, **kw):
        raise scope.RenewalRefused('board_unavailable', 'the Board could not be reached')
    monkeypatch.setattr(renewal_cli, 'renew', down)
    first = run(board, tid)
    assert first['refusal'] == 'board_unavailable' and mandates(board, tid) == 1
    monkeypatch.setattr(renewal_cli, 'renew', real)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': episode})
    board.commit()  # scope evidence newer than that mandate: reusing it would be refused mandate_stale for ever
    out = run(board, tid)
    assert out['outcome'] == 'released', out
    assert out['mandate_reused'] is False and out['mandate_comment'] != first['mandate_comment']
    assert mandates(board, tid) == 2


def test_command_line_refuses_a_stale_decision_card(board, monkeypatch, capsys):
    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: HUMAN)
    tid = spend_stopped(board)
    stale = kb.create_task(board, title='Spend budget decision: stale', assignee='default',
                           created_by='kanban-spend-budget')
    kb._append_event(board, stale, 'administrative_decision', {'root_task': tid, 'root_episode': 'stop-1'})
    board.commit()
    current = scope.renewal.key(decision.pending_episode(board, tid))
    before = written(board, tid)
    assert argv(stale, '--json') == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['refusal'] == 'stale_decision_card' and tid in out['message'] and current in out['message']
    assert 'stop-1' in out['message'] and written(board, tid) == before and status(board, tid) == 'triage'
    assert argv(tid) == 0 and status(board, tid) == 'ready'


def test_unreadable_ancestry_is_identity_unavailable(board, monkeypatch):
    from hermes_cli import kanban_comment_writer as writer
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)

    def unreadable(*a, **kw):
        raise writer.Inconclusive('pid 4242: environment unreadable')
    monkeypatch.setattr(writer, 'ancestry_environs', unreadable)
    before = written(board, tid)
    out = release.release(board, tid, writer=scope.AGENT, env=OWNER)  # no ancestry handed in: it is read, and fails
    assert out['refusal'] == 'identity_unavailable' and 'pid 4242' in out['message']
    assert written(board, tid) == before and status(board, tid) == 'triage'


def test_an_offer_another_gateway_pushed_is_not_this_boards_offer(board):
    """tasks.human_request and task_events are synced rows any gateway can write on the Board; only an
    escalation event this board wrote (no origin, or its own) makes a stamped ask an offer."""
    from hermes_cli.kanban_origin import gateway_names
    tid = spend_stopped(board)
    ask = {'release_target': release.offer(board, tid)}
    if 'origin_gateway' not in {r[1] for r in board.execute('PRAGMA table_info(task_events)')}:
        board.execute('ALTER TABLE task_events ADD COLUMN origin_gateway TEXT')
    filed(board, tid, ask)
    event = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='stranded_park_escalated'",
                          (tid,)).fetchone()[0]
    assert release.offered(board, tid, ask) == tid
    for origin, ours in (('worker-9', False), (gateway_names()[0], True), (None, True)):
        board.execute('UPDATE task_events SET origin_gateway=? WHERE id=?', (origin, event))
        board.commit()
        assert (release.offered(board, tid, ask) == tid) is ours, origin
    # A later escalation of the same card without the offer (say it became safety-restricted) withdraws it.
    kb._append_event(board, tid, 'stranded_park_escalated', {'request': True})
    board.commit()
    assert release.offered(board, tid, ask) is None


# ---- follow-up 2: a kept limit keeps its attribution; a card with no run limit over its cache-read limit ----

def stop_again(board, tid):
    """The released card ran its budget out and the dispatcher stopped it again."""
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: again'})


def newest_budget(board, tid):
    return json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spend_budget_set' "
                                    "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])


def test_a_kept_limit_is_still_kept_by_a_second_agent_release(board):
    tid = spend_stopped(board)
    scope.link(board, tid)
    budget.set_override(board, tid, runs=3, cache_read_tokens=5_000_000, by='david', reason='small and cheap',
                        writer_kind='human')
    shown = as_owner(board, tid)
    assert as_owner(board, tid, seen=max(shown['unread']))['outcome'] == 'released'
    first = newest_budget(board, tid)
    assert first['writer_kind'] == 'agent' and first['cache_read_writer_kind'] == 'human'
    assert first['cache_read_tokens'] == 5_000_000 and budget.cache_attribution(first) == 'human'
    stop_again(board, tid)
    assert as_owner(board, tid)['outcome'] == 'released'  # nothing new to read after the second stop
    second = newest_budget(board, tid)
    assert second is not first and second['cache_read_tokens'] == 5_000_000
    assert second['cache_read_writer_kind'] == 'human'
    assert budget.card_limits(board, tid, budget.limits()).cache_read_tokens == 5_000_000
    # An override that records no writer at all is carried as unknown, and still kept the next time.
    old = spend_stopped(board)
    scope.link(board, old)
    budget.set_override(board, old, runs=3, cache_read_tokens=7_000_000, by='someone', reason='before attribution')
    shown = as_owner(board, old)
    as_owner(board, old, seen=max(shown['unread']))
    assert newest_budget(board, old)['cache_read_writer_kind'] == 'unknown'
    stop_again(board, old)
    as_owner(board, old)
    assert budget.card_limits(board, old, budget.limits()).cache_read_tokens == 7_000_000


def test_carried_attribution_for_the_reviewer_and_the_courier_promote():
    assert budget.carried({}) == {'writer_kind': 'agent'}  # no cache-read limit of the card's own to attribute
    assert budget.carried({'cache_read_tokens': 5, 'writer_kind': 'human'}) == {
        'writer_kind': 'agent', 'cache_read_writer_kind': 'human'}
    assert budget.carried({'cache_read_tokens': 5}) == {'writer_kind': 'agent', 'cache_read_writer_kind': 'unknown'}
    assert budget.carried({'cache_read_tokens': 5, 'writer_kind': 'agent', 'cache_read_writer_kind': 'human'}) == {
        'writer_kind': 'agent', 'cache_read_writer_kind': 'human'}


def over_cache(monkeypatch, tokens):
    real = budget.spend

    def spend(conn, task, **kw):
        spent = real(conn, task, **kw)
        return spent._replace(tokens={**spent.tokens, 'cache_read_tokens': tokens})
    monkeypatch.setattr(budget, 'spend', spend)


def test_an_exempt_card_over_the_board_cache_limit_is_left_alone_and_not_stopped(board, monkeypatch):
    """Exempt is 0/0: the spend budget never stops it, whatever it has read. Pinned."""
    tid, _, _ = scope.stopped(board, monkeypatch)
    budget.set_override(board, tid, runs=0, cache_read_tokens=0, by='david', reason='standing card')
    over_cache(monkeypatch, budget.limits().cache_read_tokens * 3)
    out = run(board, tid)
    assert out['outcome'] == 'released' and out['budget_runs'] == 0
    assert newest_budget(board, tid)['by'] == 'david'  # untouched
    assert budget.hold(board, tid) is None and status(board, tid) == 'ready'


def test_a_card_with_no_run_limit_over_its_cache_limit_is_not_handed_back_to_be_stopped(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    budget.set_override(board, tid, runs=0, by='david', reason='no run cap; the board cache limit applies')
    over_cache(monkeypatch, budget.limits().cache_read_tokens + 1)
    shown = as_owner(board, tid)
    before = written(board, tid)
    out = as_owner(board, tid, seen=max(shown['unread']))
    assert out['refusal'] == 'budget_limit_in_force' and written(board, tid) == before  # an agent does not lift it
    out = run(board, tid)
    assert out['outcome'] == 'released'
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(0, 0)  # still no run limit; lifted
    assert budget.hold(board, tid) is None and status(board, tid) == 'ready'


def test_a_persons_run_only_override_keeps_its_deference_to_the_board_limit(board):
    """Human 3 runs / no cache value -> agent release -> stop -> agent release: the board limit still applies."""
    tid = spend_stopped(board)
    scope.link(board, tid)
    budget.set_override(board, tid, runs=3, by='david', reason='three more', writer_kind='human')
    board_limit = budget.limits().cache_read_tokens
    assert board_limit > 0
    shown = as_owner(board, tid)
    assert as_owner(board, tid, seen=max(shown['unread']))['outcome'] == 'released'
    first = newest_budget(board, tid)
    assert first['cache_read_tokens'] is None and first['cache_read_writer_kind'] == 'human'
    stop_again(board, tid)
    assert as_owner(board, tid)['outcome'] == 'released'
    second = newest_budget(board, tid)
    assert second['cache_read_tokens'] is None and second['cache_read_writer_kind'] == 'human'
    assert budget.card_limits(board, tid, budget.limits()).cache_read_tokens == board_limit


def test_an_agent_release_written_before_attribution_reads_as_unknown_not_agent(board):
    """Under the first follow-up an agent release that kept a person's limit recorded only writer_kind agent."""
    era = {'runs': 13, 'cache_read_tokens': 5_000_000, 'by': 'default', 'writer_kind': 'agent',
           'reason': 'hermes kanban release: 10 more runs (mandate comment 7)'}
    assert budget.cache_attribution(era) == 'unknown'
    assert budget.cache_attribution({**era, 'reason': 'tightening'}) == 'agent'  # an agent's own spend-budget
    assert budget.cache_attribution({**era, 'cache_read_writer_kind': 'agent'}) == 'agent'
    tid = spend_stopped(board)
    scope.link(board, tid)
    with kb.write_txn(board):
        kb._append_event(board, tid, budget.OVERRIDE_EVENT, era)
    stop_again(board, tid)
    out = as_owner(board, tid)
    assert out['outcome'] == 'released', out
    kept = newest_budget(board, tid)
    assert kept['cache_read_tokens'] == 5_000_000 and kept['cache_read_writer_kind'] == 'unknown'
    # A release that lifts the limit says so: the limit is then this writer's.
    human = spend_stopped(board)
    run(board, human)
    assert newest_budget(board, human)['cache_read_writer_kind'] == 'human'


# ---- leftovers: --seen is stateful; spend-budget keeps a person's limit ----

def shown_events(board, tid):
    return [json.loads(r[0]) for r in board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, release.SHOWN))]


def test_a_correct_first_guess_of_seen_is_refused_and_prints_the_comment(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Hold this until Friday.', writer_kind='human')
    before = written(board, tid)
    out = as_owner(board, tid, seen=said)  # the exact newest id, never printed to this caller
    assert out['refusal'] == 'budget_decision_unread' and out['unread'] == [said]
    assert 'Hold this until Friday.' in out['message'] and 'printed to this session by an earlier release refusal' in out['message']
    assert written(board, tid) == before and status(board, tid) == 'triage'
    (record,) = shown_events(board, tid)
    assert record['session'] == scope.CHAT and record['comment_ids'] == [said] and record['verb'] == 'release'
    assert record['actor_kind'] == 'agent' and isinstance(record['time'], int)
    assert as_owner(board, tid, seen=said)['outcome'] == 'released'  # now it was printed to this session


def test_a_refusal_then_a_rerun_with_the_printed_id_releases(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Only the docs fix, please.', writer_kind='human')
    out = as_owner(board, tid)
    assert out['refusal'] == 'budget_decision_unread' and f'--seen {said}' in out['message']
    assert as_owner(board, tid, seen=said)['outcome'] == 'released'


def test_another_sessions_refusal_does_not_count(board, monkeypatch):
    from hermes_cli import kanban_scope_delegate as delegates
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    delegates.grant(board, session=TRIAGE, reason='triage chat may unblock cards', writer=scope.HUMAN)
    said = kb.add_comment(board, tid, 'default', 'Wait for my review.', writer_kind='human')
    assert as_triage(board, tid)['refusal'] == 'budget_decision_unread'  # printed to the triage chat
    assert [e['session'] for e in shown_events(board, tid)] == [TRIAGE]
    out = as_owner(board, tid, seen=said)  # the owning chat never saw it
    assert out['refusal'] == 'budget_decision_unread' and 'Wait for my review.' in out['message']
    assert status(board, tid) == 'triage'
    assert as_owner(board, tid, seen=said)['outcome'] == 'released'


def test_a_shown_record_another_gateway_pushed_does_not_count(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Hold.', writer_kind='human')
    with kb.write_txn(board):
        kb._append_event(board, tid, release.SHOWN, {'version': 1, 'session': scope.CHAT, 'comment_ids': [said]})
        if 'origin_gateway' not in {r[1] for r in board.execute('PRAGMA table_info(task_events)')}:
            board.execute('ALTER TABLE task_events ADD COLUMN origin_gateway TEXT')
        board.execute('UPDATE task_events SET origin_gateway=? WHERE task_id=? AND kind=?',
                      ('worker-9', tid, release.SHOWN))
    assert as_owner(board, tid, seen=said)['refusal'] == 'budget_decision_unread'


def test_a_caller_without_a_session_cannot_satisfy_seen(board):
    tid = spend_stopped(board)
    said = kb.add_comment(board, tid, 'default', 'Hold.', writer_kind='human')
    nobody = ('agent', None, 'runner:systemd:x.service')
    for seen in (None, said, said):
        try:
            release.read_first(board, tid, decision.pending_episode(board, tid), nobody, seen, None)
        except release.Refusal as exc:
            assert exc.code == 'budget_decision_unread' and 'no session id' in str(exc)
        else:
            raise AssertionError('a session-less agent passed --seen')


def spend_budget(board, monkeypatch, tid, *flags, writer=scope.AGENT):
    """``hermes kanban spend-budget`` as ``writer``: ``(exit code, stderr)``."""
    import argparse
    import contextlib
    import io
    from hermes_cli import kanban_spend_budget_cli as cli
    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: writer)
    parser = argparse.ArgumentParser()
    parser.add_argument('task_id')
    parser.add_argument('--seen', dest='seen_comment_id', type=int)
    parser.add_argument('--runs', type=int)
    parser.add_argument('--cache-read-tokens', type=int)
    parser.add_argument('--exempt', action='store_true')
    parser.add_argument('--clear', action='store_true')
    parser.add_argument('--reason')
    parser.add_argument('--json', action='store_true')
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = cli.command(parser.parse_args([tid, *flags]))
    return rc, err.getvalue()


def with_person_budget(board, *, stop=False):
    """A card whose budget a person set (runs 3, 5M cache-read tokens); ``stop``: then spend-stopped."""
    tid = kb.create_task(board, title='Budgeted work', assignee='default')
    budget.set_override(board, tid, runs=3, cache_read_tokens=5_000_000, by='david', reason='small and cheap',
                        writer_kind='human')
    if stop:
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: 3 runs'})
    return tid


def test_an_agent_restating_a_persons_budget_keeps_its_attribution_and_a_release_keeps_the_limit(board, monkeypatch):
    tid = with_person_budget(board, stop=True)
    scope.link(board, tid)
    rc, err = spend_budget(board, monkeypatch, tid, '--runs', '3', '--cache-read-tokens', '5000000',
                           '--reason', 'restating')
    assert rc == 0, err
    restated = newest_budget(board, tid)
    assert restated['writer_kind'] == 'agent' and restated['cache_read_writer_kind'] == 'human'
    assert restated['runs_writer_kind'] == 'human' and budget.cache_attribution(restated) == 'human'
    shown = as_owner(board, tid)
    out = as_owner(board, tid, seen=max(shown['unread']))
    assert out['outcome'] == 'released', out
    assert budget.card_limits(board, tid, budget.limits()).cache_read_tokens == 5_000_000  # the person's cap stays


def test_an_agent_tightening_a_persons_limit_keeps_its_attribution(board, monkeypatch):
    tid = kb.create_task(board, title='Budgeted work', assignee='default')
    budget.set_override(board, tid, runs=20, cache_read_tokens=5_000_000, by='david', reason='cheap')
    rc, err = spend_budget(board, monkeypatch, tid, '--runs', '10', '--cache-read-tokens', '1000000',
                           '--reason', 'narrower')
    assert rc == 0, err
    tightened = newest_budget(board, tid)
    assert tightened['cache_read_writer_kind'] == 'unknown' and tightened['runs_writer_kind'] == 'unknown'
    # ... so lifting it again is still a person's limit being lifted
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--reason', 'lift')
    assert rc == 4 and 'budget_decision_unread' in err and newest_budget(board, tid) == tightened


@pytest.mark.parametrize('flags', [('--cache-read-tokens', '0', '--runs', '3'),
                                   ('--cache-read-tokens', '9000000', '--runs', '3'),
                                   ('--runs', '3'),  # the cache-read field falls back to the larger board limit
                                   ('--runs', '30', '--cache-read-tokens', '5000000'),
                                   ('--exempt',), ('--clear',)])
def test_an_agent_lifting_a_persons_limit_must_read_first(board, monkeypatch, flags):
    tid = with_person_budget(board)
    person = newest_budget(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Keep it cheap.', writer_kind='human')
    rc, err = spend_budget(board, monkeypatch, tid, *flags, '--seen', str(said), '--reason', 'lift')  # a guess
    assert rc == 4 and 'budget_decision_unread' in err and 'Keep it cheap.' in err
    assert 'small and cheap' in err  # the person's own budget comment is printed
    assert newest_budget(board, tid) == person
    newest = max(shown_events(board, tid)[-1]['comment_ids'])
    assert f'--seen {newest}' in err
    rc, err = spend_budget(board, monkeypatch, tid, *flags, '--seen', str(newest), '--reason', 'lift',
                           writer=('agent', 'another-session', '1:HERMES_SESSION_ID'))
    assert rc == 4 and newest_budget(board, tid) == person  # another session's refusal does not count
    rc, err = spend_budget(board, monkeypatch, tid, *flags, '--seen', str(newest), '--reason', 'lift')
    assert rc == 0, err
    assert newest_budget(board, tid)['writer_kind'] == 'agent'


def test_a_human_spend_budget_is_unchanged(board, monkeypatch):
    tid = with_person_budget(board)
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--reason', 'standing', writer=scope.HUMAN)
    assert rc == 0, err
    lifted = newest_budget(board, tid)
    assert lifted['writer_kind'] == 'human' and 'cache_read_writer_kind' not in lifted
    assert 'runs_writer_kind' not in lifted and not shown_events(board, tid)


def test_an_agent_replacing_an_agents_limit_needs_no_read(board, monkeypatch):
    tid = kb.create_task(board, title='Budgeted work', assignee='default')
    budget.set_override(board, tid, runs=3, cache_read_tokens=5_000_000, by='bot', reason='x', writer_kind='agent')
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--reason', 'standing')
    assert rc == 0, err
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(0, 0)


def test_a_release_refusal_does_not_stand_in_for_the_spend_budget_read(board, monkeypatch):
    """R1: what release printed (the human comment) leaves out the person's budget note spend-budget must show."""
    tid = with_person_budget(board, stop=True)
    scope.link(board, tid)
    said = kb.add_comment(board, tid, 'default', 'Keep it under the cap.', writer_kind='human')
    assert as_owner(board, tid)['unread'] == [said]  # release's own refusal: printed [said] only
    person = newest_budget(board, tid)
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--seen', str(said), '--reason', 'lift')
    assert rc == 4 and 'small and cheap' in err and newest_budget(board, tid) == person
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--seen', str(said), '--reason', 'lift')
    assert rc == 0, err  # read through spend-budget's own refusal


def test_a_spend_budget_refusal_does_not_stand_in_for_the_release_read(board, monkeypatch):
    """R2: a person's comment only release requires (after the stop, before the budget) must be printed by release."""
    tid = kb.create_task(board, title='Budgeted work', assignee='default')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: 3 runs'})
    scope.link(board, tid)
    legal = kb.add_comment(board, tid, 'default', 'DO NOT RELEASE until legal signs off.', writer_kind='human')
    budget.set_override(board, tid, runs=3, cache_read_tokens=5_000_000, by='david', reason='small and cheap',
                        writer_kind='human')
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--reason', 'lift')
    assert rc == 4 and 'DO NOT RELEASE' not in err
    printed = shown_events(board, tid)[-1]['comment_ids']
    assert legal not in printed
    out = as_owner(board, tid, seen=max(printed))
    assert out['refusal'] == 'budget_decision_unread' and 'DO NOT RELEASE until legal signs off.' in out['message']
    assert status(board, tid) == 'triage'
    assert as_owner(board, tid, seen=max(out['unread']))['outcome'] == 'released'


def test_an_agent_release_keeps_a_persons_run_limit_attribution(board, monkeypatch):
    tid = with_person_budget(board, stop=True)
    scope.link(board, tid)
    assert as_owner(board, tid)['outcome'] == 'released'  # nothing after the stop to read
    released = newest_budget(board, tid)
    assert released['writer_kind'] == 'agent' and released['runs_writer_kind'] == 'human'
    before = newest_budget(board, tid)
    rc, err = spend_budget(board, monkeypatch, tid, '--runs', '0', '--cache-read-tokens', '5000000',
                           '--reason', 'unlimited runs')
    assert rc == 4 and 'budget_decision_unread' in err and newest_budget(board, tid) == before



@pytest.mark.parametrize('shape', ['pre_attribution', 'note_not_synced'])
def test_a_person_budget_without_a_readable_note_is_itself_a_required_read(board, monkeypatch, shape):
    tid = kb.create_task(board, title='Budgeted work', assignee='default')
    payload = {'runs': 3, 'cache_read_tokens': 5_000_000, 'by': 'david', 'reason': 'cheap'}
    if shape == 'note_not_synced':
        payload['writer_kind'] = 'human'
    with kb.write_txn(board):  # the event alone: no note comment on this board
        kb._append_event(board, tid, budget.OVERRIDE_EVENT, payload)
    event_id = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?",
                             (tid, budget.OVERRIDE_EVENT)).fetchone()[0]
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--reason', 'lift')
    assert rc == 4 and 'budget_decision_unread' in err
    assert f'Budget event {event_id}' in err and 'set by david: cheap' in err and 'runs 3' in err
    assert newest_budget(board, tid) == payload
    (record,) = shown_events(board, tid)
    assert record['event_ids'] == [event_id] and record['verb'] == 'spend-budget'
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--seen', str(event_id), '--reason', 'lift',
                           writer=('agent', 'another-session', '1:HERMES_SESSION_ID'))
    assert rc == 4 and newest_budget(board, tid) == payload  # another session never saw it
    rc, err = spend_budget(board, monkeypatch, tid, '--exempt', '--seen', str(event_id), '--reason', 'lift')
    assert rc == 0, err
    assert budget.card_limits(board, tid, budget.limits()) == budget.Limits(0, 0)
