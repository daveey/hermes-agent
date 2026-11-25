"""Worker stop rules: what a released card's next run reads, and what a scope stop must name. [daveey]

On 2026-11-24/25 the fleet had 82 scope stops on 55 cards and 70 of 137 releases were followed by another stop.
The next run's prompt carried the release mandate under ~30k characters of earlier stop summaries, framed "stop
again if the work needs more", and ``kanban_retain(reason='scope_stopped')`` took any free text."""
import json
from contextlib import contextmanager

import pytest

from agent import prompt_builder
from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_retain as retain
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_release as rel
from tests.hermes_cli import test_kanban_scope_renewal as scope
from tools import kanban_retain_tools as tool

board = scope.board


def _section(context: str, title: str) -> str:
    return context.split(title, 1)[1].split('\n## ', 1)[0]


# --- the release mandate leads the next run's context -------------------------------------------------------


def test_released_scope_stop_puts_the_mandate_above_prior_stops_without_stop_again_framing(board, monkeypatch):
    tid, _, _ = scope.stopped(board, monkeypatch)  # leaves a retained prior attempt with its stop summary
    out = rel.run(board, tid, runs=7)
    assert out['outcome'] == 'released'
    context = kb.build_worker_context(board, tid)
    assert '## Release mandate (governs this run)' in context
    section = _section(context, '## Release mandate (governs this run)')
    assert release.mandate_text(7) in section
    assert 'not instructions' in section and '`kanban_complete`' in section
    assert context.index('## Release mandate') < context.index('## Prior attempts on this task')
    assert context.index('## Release mandate') < context.index('## Comment thread')
    # The renewal written by the release is the same comment: no second copy with the old framing.
    assert '## Scope renewal mandate' not in context
    assert 'stop again if the work needs more' not in context


def test_released_budget_stop_shows_the_mandate_although_no_scope_renewal_exists(board):
    tid = rel.spend_stopped(board)
    out = rel.run(board, tid, note='Desktop Unblock, operation op-1')
    assert out['outcome'] == 'released' and out['scope_renewed'] is False
    context = kb.build_worker_context(board, tid)
    section = _section(context, '## Release mandate (governs this run)')
    assert release.MARK in section and 'Desktop Unblock, operation op-1' in section
    assert 'by a human operator' in section


def test_a_stop_after_the_release_hides_the_stale_mandate(board):
    tid = rel.spend_stopped(board)
    assert rel.run(board, tid)['outcome'] == 'released'
    with kb.write_txn(board):
        kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: 25 runs'})
    assert release.current_mandate(board, tid) is None
    assert '## Release mandate' not in kb.build_worker_context(board, tid)


def test_an_execution_restriction_after_the_release_hides_the_mandate(board, monkeypatch):
    tid = rel.spend_stopped(board)
    assert rel.run(board, tid)['outcome'] == 'released'
    assert release.current_mandate(board, tid) is not None
    monkeypatch.setattr(decision, 'execution_restricted', lambda conn, task: True)
    assert release.current_mandate(board, tid) is None
    assert '## Release mandate' not in kb.build_worker_context(board, tid)


def test_the_release_header_keeps_card_safety_exclusions_and_human_stops(board):
    tid = rel.spend_stopped(board)
    rel.run(board, tid)
    section = _section(kb.build_worker_context(board, tid), '## Release mandate (governs this run)')
    assert "Safety exclusions in the card body and a human's stop instructions still hold." in section


def test_a_plain_renewal_keeps_its_own_section(board, monkeypatch):
    """A renew-scope mandate that is not a release keeps its narrow framing (a human may have meant it)."""
    tid, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    promoted, why = kb.promote_task(board, tid, actor='fixture', reason='renewed scope')
    assert promoted, why
    context = kb.build_worker_context(board, tid)
    assert '## Scope renewal mandate' in context and '## Release mandate' not in context


# --- an agent's release note cannot re-narrow the stop list ------------------------------------------------


def test_agent_mandate_says_note_stop_conditions_do_not_add_and_a_humans_does_not(board, monkeypatch):
    agent = release.mandate_text(10, 'Stop only if the keyboard defect fails 3 times.', agent=True, budget='24 runs')
    assert 'Stop conditions in a note below do not add to the stop list that follows.' in agent
    assert agent.index('do not add to the stop list') < agent.index('Stop only on:')
    assert 'do not add to the stop list' not in release.mandate_text(10, 'x')  # a human's wording is whole


# --- the worker prompt ------------------------------------------------------------------------------------


def test_worker_guidance_lists_the_stops_and_says_complete_not_retain():
    text = prompt_builder.KANBAN_GUIDANCE
    assert '**Stopping a scope is the exception.**' in text
    for stop in ('a provider safety refusal', 'a step that needs David physically', 'the same error three times',
                 'the run budget'):
        assert stop in text
    assert 'review CHANGES, red or pending CI and sandbox refusals are work inside the round' in text
    assert 'When the acceptance is met, finish with `kanban_complete` (or `kanban_request_review`), never `kanban_retain`' in text
    assert 'stop_on=...' in text
    assert 'safety exclusions in the card body always do' in text


# --- kanban_retain: a scope stop names an allowed stop --------------------------------------------------------


@pytest.fixture
def captured(board, monkeypatch):
    """The real tool handler on ``board`` with the retain call itself recorded instead of run."""
    from tools import kanban_tools
    calls = []

    @contextmanager
    def _board(_):
        yield None, board

    monkeypatch.setattr(kanban_tools, '_board', _board)
    monkeypatch.setattr(retain, 'retain', lambda conn, **kw: calls.append(kw) or {'ok': True, 'terminal_for_run': True})
    return calls


def test_schema_offers_the_stop_categories():
    props = tool.SCHEMA['parameters']['properties']
    assert props['stop_on']['enum'] == list(retain.STOP_ON)
    assert 'stop_on' not in tool.SCHEMA['parameters']['required']
    assert 'kanban_complete' in tool.SCHEMA['description'] and 'needs stop_on' in tool.SCHEMA['description']


def test_scope_stop_without_stop_on_is_refused_and_nothing_is_retained(captured):
    out = json.loads(tool.handle(dict(scope.ARGS)))
    assert out['ok'] is False and out['terminal_for_run'] is False
    assert 'needs stop_on' in out['error'] and 'kanban_complete' in out['error']
    assert 'review CHANGES' in out['error'] and 'kanban_complete' in out['next_step']
    assert captured == []


def test_scope_stop_with_free_text_category_is_refused(captured):
    out = json.loads(tool.handle({**scope.ARGS, 'stop_on': 'pr_landed'}))
    assert out['ok'] is False and captured == []


def test_scope_stop_with_a_category_carries_it_in_the_summary(captured):
    out = json.loads(tool.handle({**scope.ARGS, 'stop_on': 'same_error_three_times'}))
    assert out['ok'] is True
    assert len(captured) == 1 and 'stop_on' not in captured[0]
    assert captured[0]['summary'] == '[stop_on=same_error_three_times] ' + scope.ARGS['summary']


def test_a_card_safety_exclusion_is_an_allowed_stop_and_a_self_tagged_summary_is_tagged_once(captured):
    assert 'card_safety_exclusion' in retain.STOP_ON
    summary = '[stop_on=card_safety_exclusion] Next step would touch production keys the card excludes.'
    out = json.loads(tool.handle({**scope.ARGS, 'summary': summary, 'stop_on': 'card_safety_exclusion'}))
    assert out['ok'] is True and captured[0]['summary'] == summary


def test_other_reasons_pass_unchanged_and_refuse_a_stop_on(captured):
    args = {**scope.ARGS, 'reason': 'capability_unavailable'}
    assert json.loads(tool.handle(dict(args)))['ok'] is True
    assert len(captured) == 1 and {k: v for k, v in captured[0].items() if k != 'board'} == args
    refused = json.loads(tool.handle({**args, 'stop_on': 'run_budget'}))
    assert refused['ok'] is False and len(captured) == 1


def test_a_tagged_summary_over_the_bound_is_refused(captured):
    out = json.loads(tool.handle({**scope.ARGS, 'summary': 'x' * 1990, 'stop_on': 'run_budget'}))
    assert out['ok'] is False and 'shorten' in out['error'] and captured == []


def test_end_to_end_the_stop_record_names_its_category(board, monkeypatch):
    """Through the real retain: the stop event a census counts begins with the category."""
    from tests.hermes_cli import test_kanban_containment_actor as support
    from tools import kanban_tools

    @contextmanager
    def _board(_):
        yield None, board

    monkeypatch.setattr(kanban_tools, '_board', _board)
    tid, _, session = support.bind(board, monkeypatch)
    with scoped_current_session_id(session):
        out = json.loads(tool.handle({**scope.ARGS, 'stop_on': 'needs_david_physically'}))
    assert out['ok'] is True and out['terminal_for_run'] is True, out
    reason = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                      (tid, decision.EVENT)).fetchone()[0])['reason']
    assert reason.startswith('[stop_on=needs_david_physically] ')


def test_rejected_retain_source_still_matches_a_tagged_call():
    from hermes_cli.kanban_rejected_retain_source import _sent
    args = {**scope.ARGS, 'stop_on': 'run_budget'}
    assert _sent(args) == retain.tool_args(args)
    legacy = dict(scope.ARGS)  # a scope stop written before stop_on existed is compared as written
    assert _sent(legacy) == legacy


# --- follow-up: human instructions stay in view; creation order; brief keeps the tag -------------------------


HUMAN_SECTION = '## Instructions David wrote on this card (older than the thread below)'


def _no_worker_env(monkeypatch):
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID'):
        monkeypatch.delenv(name, raising=False)


def _mark(board, tid, cid, **fields):
    """Add ``fields`` to comment ``cid``'s ``commented`` event (as the relay/decision writers record them)."""
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='commented' "
                        "AND json_extract(payload,'$.comment_id')=?", (tid, cid)).fetchone()
    with kb.write_txn(board):
        board.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps({**json.loads(row[1]), **fields}), row[0]))


def _filler(board, tid, n=40):
    for i in range(n):
        kb.add_comment(board, tid, 'default', f'progress note {i}', writer_kind='agent', writer_session='s')


def test_an_old_human_do_not_deploy_comment_stays_in_the_prompt(board, monkeypatch):
    _no_worker_env(monkeypatch)
    tid = rel.spend_stopped(board)
    kb.add_comment(board, tid, 'david', 'Do not deploy to production until I say so.', writer_kind='human')
    _filler(board, tid)
    rel.run(board, tid)
    context = kb.build_worker_context(board, tid)
    section = _section(context, HUMAN_SECTION)
    assert 'Do not deploy to production until I say so.' in section and 'still apply' not in section
    assert context.index('## Release mandate') < context.index(HUMAN_SECTION) < context.index('## Comment thread')


def test_relayed_and_recorded_answers_are_davids_words(board, monkeypatch):
    _no_worker_env(monkeypatch)
    tid = rel.spend_stopped(board)
    relayed = kb.add_comment(board, tid, 'default', 'Relayed: keep the old schema.', writer_kind='agent',
                             writer_session='s')
    _mark(board, tid, relayed, relayed_human={'session': 's', 'message_id': 'm1', 'at': 1})
    decided = kb.add_comment(board, tid, 'default', 'Recorded answer: option B.', writer_kind='agent',
                             writer_session='s')
    _mark(board, tid, decided, recorded_decision={'ask': 'a1'})
    _filler(board, tid)
    rel.run(board, tid)
    section = _section(kb.build_worker_context(board, tid), HUMAN_SECTION)
    assert 'keep the old schema' in section and 'option B' in section


@pytest.mark.parametrize('author,body,kind', [
    ('default', 'Legacy agent note: stop if CI is red.', None),                    # pre-writer_kind agent note
    ('default', 'CHANGES REQUESTED: stop after this round.', 'agent'),             # operator verb by a chat
    ('default', 'REOPENED by the owning chat.', None),
    ('default', 'Closed by hermes kanban release.', None),
    ('update-pause', 'Updates paused; do not restart.', None),                     # system writers outside
    ('upstream-merge', 'Upstream merge staged.', None),                            # is_system_author
    ('hub-cpu-advisory', 'Hub CPU high; stop heavy work.', None),
    ('lane-planner', 'Lane planned.', None),
    ('pickup-stall-detector', 'Pickup stalled.', None),
])
def test_comments_without_proof_of_david_stay_out(board, monkeypatch, author, body, kind):
    _no_worker_env(monkeypatch)
    tid = rel.spend_stopped(board)
    kb.add_comment(board, tid, author, body, writer_kind=kind, writer_session='s' if kind == 'agent' else None)
    _filler(board, tid)
    rel.run(board, tid)
    assert body not in kb.build_worker_context(board, tid)


def test_a_synced_comment_with_no_commented_event_stays_out(board, monkeypatch):
    _no_worker_env(monkeypatch)
    tid = rel.spend_stopped(board)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                      (tid, 'david', 'Orphan: stop everything.', 2))
    _filler(board, tid)
    rel.run(board, tid)
    assert 'Orphan: stop everything.' not in kb.build_worker_context(board, tid)


def test_an_ordinary_card_prompt_is_unchanged(board, monkeypatch):
    _no_worker_env(monkeypatch)
    tid = kb.create_task(board, title='Plain work', assignee='default')
    kb.add_comment(board, tid, 'david', 'Do not deploy.', writer_kind='human')
    _filler(board, tid)
    context = kb.build_worker_context(board, tid)
    assert HUMAN_SECTION not in context and 'Do not deploy.' not in context  # 40 back: out of the thread tail


def test_human_instructions_are_bounded_and_count_the_rest(board, monkeypatch):
    _no_worker_env(monkeypatch)
    tid = rel.spend_stopped(board)
    for i in range(13):
        kb.add_comment(board, tid, 'david', f'human rule {i} ' + 'x' * 2000, writer_kind='human')
    _filler(board, tid, 30)
    rel.run(board, tid)
    section = _section(kb.build_worker_context(board, tid), HUMAN_SECTION)
    assert '_(3 more not shown)_' in section
    assert 'human rule 12 ' in section and 'human rule 2 ' not in section
    assert section.count('x' * 1000) == 10 and 'x' * 1100 not in section  # 1 KiB each


def _swap_ids(conn, a, b):
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET id=-1 WHERE id=?', (a,))
        conn.execute('UPDATE task_events SET id=? WHERE id=?', (a, b))
        conn.execute('UPDATE task_events SET id=? WHERE id=-1', (b,))


def test_release_and_stop_are_ordered_by_creation_not_arrival(board):
    tid = rel.spend_stopped(board)
    rel.run(board, tid)
    (rid, rts), = board.execute("SELECT id, created_at FROM task_events WHERE task_id=? AND kind='stop_released'",
                                (tid,)).fetchall()
    with kb.write_txn(board):  # a stop written 10 s after the release ...
        kb._append_event(board, tid, decision.EVENT, {'reason': 'Spend budget: 25 runs'})
        sid = board.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
        board.execute('UPDATE task_events SET created_at=? WHERE id=?', (rts + 10, sid))
    _swap_ids(board, rid, sid)  # ... that arrived before it
    assert release._release_event(board, tid) is None and release.current_mandate(board, tid) is None
    # Reversed: the stop was created before the release although it arrived after it.
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=? AND created_at=?",
                      (rts - 10, tid, decision.EVENT, rts + 10))
    assert release._release_event(board, tid) is not None


def test_stranded_brief_keeps_the_stop_on_tag():
    from hermes_cli.kanban_stranded_brief import reason
    entry = {'kind': 'decision_retained', 'payload': {'disposition': {
        'summary': '[stop_on=same_error_three_times] the ship_ledger write failed three times'}}}
    assert reason(entry) == '[stop_on=same_error_three_times] the ship ledger write failed three times'


def test_untouched_since_reads_creation_order_on_a_reordered_replica(board):
    tid = rel.spend_stopped(board)
    rel.run(board, tid)
    record = release._release_event(board, tid)
    rts = board.execute('SELECT created_at FROM task_events WHERE id=?', (record['_id'],)).fetchone()[0]
    with kb.write_txn(board):  # the state a refused promotion leaves: the decision recorded, nothing after it
        board.execute("DELETE FROM task_events WHERE task_id=? AND id>? AND kind NOT IN ('commented')",
                      (tid, record['_id']))
    assert release._untouched_since(board, tid, record)
    with kb.write_txn(board):  # a block created after the release that arrived before it
        kb._append_event(board, tid, 'blocked', {'reason': 'later block'})
        bid = board.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
        board.execute('UPDATE task_events SET created_at=? WHERE id=?', (rts + 10, bid))
    _swap_ids(board, record['_id'], bid)
    record = release._release_event(board, tid)
    assert record is not None and not release._untouched_since(board, tid, record)


def test_the_read_first_check_and_the_context_share_one_definition():
    assert release.by_person({'writer_kind': 'human'}) and release.by_person({'recorded_decision': {}})
    assert release.by_person({'writer_kind': 'agent', 'relayed_human': {'session': 's'}})
    assert not release.by_person({}) and not release.by_person({'writer_kind': 'agent'})
