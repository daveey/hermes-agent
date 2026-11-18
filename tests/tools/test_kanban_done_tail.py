"""Done-tail parks publish progress without misclassifying required acceptance as complete."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.tools import test_kanban_watch_park as helpers
from hermes_cli import kanban_done_tail as tail

board = helpers.board
INCIDENT = json.loads((Path(__file__).parents[1] / 'fixtures/kanban_done_tail_171760.json').read_text())
REASON = INCIDENT['reason']


def notices(conn, tid):
    return conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?',
                        (tid, tail.NOTICE_KIND)).fetchall()


@pytest.mark.parametrize('operation', ['schedule', 'block'])
def test_historical_171760_publishes_comment_and_deliverable_notice(board, monkeypatch, operation):
    from hermes_cli import kanban_db_connect as kbc, kanban_wake_check as kwc, kanban_db_notify as kbn
    from tui_gateway import session_notifications as sn
    from gateway import kanban_watchers_notifier as gw
    from tools import kanban_tools as kt
    # Keep historical probe bytes, but simulate its pending result rather than execute a Mac script.
    monkeypatch.setattr(kwc, '_run_probe', lambda *a, **k: (1, ''))
    tid = helpers._chat_card(board)
    args = dict(task_id=tid, reason=REASON, wake_check=INCIDENT['wake_check'],
                check_every=INCIDENT['check_every'],
                awaiting={k: INCIDENT['awaiting'][k] for k in ('kind', 'ref', 'why')})
    if operation == 'block':
        args['kind'] = 'time'
    result = json.loads(getattr(kt, '_handle_' + operation)(args))
    assert result.get('status') == 'scheduled', result
    with kbc.connect() as conn:
        task = board.get_task(conn, tid)
        assert task.status == 'scheduled'
        assert len(notices(conn, tid)) == 1
        comments = conn.execute('SELECT body FROM task_comments WHERE task_id=?', (tid,)).fetchall()
        assert len(comments) == 1 and REASON in comments[0][0]
        # Exercise the real subscription cursor: the ordinary scheduled event is silent, this one is claimable.
        sub = kbn.list_notify_subs(conn, tid)[0]
        _, _, events = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform='tui',
                                                      chat_id=helpers.CHAT, thread_id='',
                                                      kinds=sn._KANBAN_NOTIFY_KINDS)
        event = next(ev for ev in events if ev.kind == tail.NOTICE_KIND)
        text = sn._format_kanban_event_text(sub, task, event, 'default')
        assert REASON in text and 'not a completion' in text and 'watch=true' in text
        msg, handoff, _ = gw._EVENT_FORMATTERS[event.kind](event, SimpleNamespace(head=tid))
        assert REASON in msg and REASON in handoff
        assert event.kind in gw.TERMINAL_KINDS and event.kind in gw._WAKE_KINDS
    assert helpers._open_for_chat(board)[0] == 1  # no automatic completion or watch flag


def test_watch_and_inherited_watch_preserve_chat_closeout_without_notice(board):
    from hermes_cli import kanban_db_connect as kbc
    tid = helpers._chat_card(board)
    result = helpers._schedule(tid, 1, reason=REASON, watch=True)
    assert result.get('watch') is True, result
    assert helpers._open_for_chat(board) == (0, [])
    with kbc.connect() as conn:
        assert not notices(conn, tid)
        assert board.unblock_task(conn, tid)
    result = helpers._schedule(tid, 1, reason=REASON)
    assert result.get('watch') is True, result
    with kbc.connect() as conn:
        assert not notices(conn, tid)


@pytest.mark.parametrize('operation', ['schedule', 'block'])
def test_validated_card_producer_remains_a_plain_park(board, monkeypatch, operation):
    from hermes_cli import kanban_awaiting as kaw, kanban_db_connect as kbc
    # The normal gate owns remote-card validation; exercise its validated record at our new seam.
    monkeypatch.setattr(kaw, 'gate', lambda *a, **k: {'kind': 'card', 'ref': 't_producer', 'gateway': 'peer'})
    monkeypatch.setattr(kaw, 'card_probe', lambda record: None)
    tid = helpers._chat_card(board)
    with kbc.connect() as conn:
        args = dict(reason=REASON, clock_only=True)
        if operation == 'block':
            args['kind'] = 'time'
        assert getattr(board, operation + '_task')(conn, tid, **args)
        assert board.get_task(conn, tid).status == 'scheduled'
        assert not notices(conn, tid)
    assert helpers._open_for_chat(board)[0] == 1


@pytest.mark.parametrize('operation', ['schedule', 'block'])
def test_failed_park_does_not_publish_notice(board, operation):
    from hermes_cli import kanban_db_connect as kbc
    tid = helpers._chat_card(board)
    with kbc.connect() as conn:
        args = dict(reason=REASON, clock_only=True, expected_run_id=999)
        if operation == 'block':
            args['kind'] = 'time'
        assert not getattr(board, operation + '_task')(conn, tid, **args)
        assert not notices(conn, tid)
        assert not conn.execute('SELECT 1 FROM task_comments WHERE task_id=?', (tid,)).fetchone()


@pytest.mark.parametrize('operation', ['schedule', 'block'])
def test_notice_failure_rolls_back_park_and_comment(board, monkeypatch, operation):
    from hermes_cli import kanban_db_connect as kbc
    tid = helpers._chat_card(board)
    append = board._append_event

    def fail_notice(conn, task_id, kind, *args, **kwargs):
        if kind == tail.NOTICE_KIND:
            raise RuntimeError('injected notice persistence failure')
        return append(conn, task_id, kind, *args, **kwargs)

    monkeypatch.setattr(board, '_append_event', fail_notice)
    with kbc.connect() as conn:
        args = dict(reason=REASON, clock_only=True)
        if operation == 'block':
            args['kind'] = 'time'
        with pytest.raises(RuntimeError, match='injected notice'):
            getattr(board, operation + '_task')(conn, tid, **args)
        assert board.get_task(conn, tid).status == 'ready'
        assert not conn.execute('SELECT 1 FROM task_comments WHERE task_id=?', (tid,)).fetchone()
        assert not notices(conn, tid)


@pytest.mark.parametrize('reason', ['Waiting for CI', 'Wait until PR is merged',
                                  'Waiting for the shipped release to install', None])
def test_plain_waits(reason):
    assert not tail.needs_notice(reason)


@pytest.mark.parametrize('phrase', ['Everything else is done', 'Only step left', 'only thing left',
                                    'only item left', 'Only remaining check', 'It is now shipped',
                                    'PR was merged', 'Fix has been shipped', 'PR already merged',
                                    'Merged PR; waiting for cleanup'])
def test_done_phrases_and_exemptions(phrase):
    assert tail.needs_notice(phrase)
    assert not tail.needs_notice(phrase, watch=True)
    assert not tail.needs_notice(phrase, awaiting={'kind': 'card', 'ref': 't_producer'})
    assert tail.needs_notice(phrase, awaiting={'kind': 'ci', 'ref': 'run'})


def test_pending_acceptance_report_is_not_completion(board):
    # Real corpus counterexample to a hard refusal: this gate is required, not optional cleanup.
    reason = 'Only remaining RULE-6 gate is the Translation Reviewer fa pass.'
    tid = helpers._chat_card(board)
    result = helpers._schedule(tid, 1, reason=reason)
    assert result.get('status') == 'scheduled', result
    assert helpers._open_for_chat(board)[0] == 1


def test_long_report_keeps_full_comment_and_bounded_notice(board):
    from hermes_cli import kanban_db_connect as kbc
    reason = 'Everything else is done: ' + 'x' * 40000 + 'only cleanup remains'
    tid = helpers._chat_card(board)
    result = helpers._schedule(tid, 1, reason=reason)
    assert result.get('status') == 'scheduled', result
    with kbc.connect() as conn:
        comment = conn.execute('SELECT body FROM task_comments WHERE task_id=?', (tid,)).fetchone()[0]
        assert reason in comment
        payload = json.loads(notices(conn, tid)[0][0])
    rendered = tail.notice_text(payload)
    assert len(rendered) < 9000 and 'full report in the card comment' in rendered
    assert 'Everything else is done:' in rendered and 'only cleanup remains' in rendered


def test_stale_notice_is_relabelled_after_wake(board):
    from gateway import kanban_wake_fold as fold
    from hermes_cli import kanban_db_connect as kbc
    tid = helpers._chat_card(board)
    assert helpers._schedule(tid, 1, reason=REASON).get('status') == 'scheduled'
    card = {'task_id': tid, 'kinds': [tail.NOTICE_KIND],
            'text': f'[kanban] Task {tid} reported progress while parked; not completed.'}
    assert fold.refresh_card(card) == card
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
    refreshed = fold.refresh_card(card)
    assert refreshed['kinds'] == ['ready']
    assert 'Live status: ready' in refreshed['text']
