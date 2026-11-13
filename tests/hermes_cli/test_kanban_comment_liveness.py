"""A comment on a ``running`` card whose worker exited must not promise a live run. [daveey]

Incident (2026-11-13, hermes-worker-6, t_01a1d563324d7e8e run 11569): the continued worker ended its turn rc=0 at
15:55, both pids dead, run row still ``running``. ``hermes kanban comment`` at 16:05 answered "The card has a live
worker run: it gets this note at its next model call"; no process would ever make that call.
"""
import argparse
import json
import os

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_comment_liveness import EXITED_NOTE, worker_exited
from tests.hermes_cli import test_kanban_owner_restart_failed_receipt as shapes
from tools import kanban_tools as kt

board = shapes.board
LIVE_RUN_PROMISE = "gets this note at its next model call"


def _exited_continued_run(conn, context):
    shapes._failed_child(conn, context)
    shapes.book(context, True)
    shapes._continued_turn(context, 0, "Tell me how you'd like to proceed")


def test_the_cli_comment_on_an_exited_worker_says_the_note_waits(board, capsys, monkeypatch):
    conn, context = board
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    _exited_continued_run(conn, context)
    assert kb.live_run_id(conn, context['task_id']) == context['run_id']  # still ``running`` on the board
    rc = kcli._cmd_comment(argparse.Namespace(task_id=context['task_id'], text=['are', 'you', 'there'],
                                              max_len=None, author='me', origin_gateway=None, _slash_typed=True))
    printed = capsys.readouterr().out
    assert rc == 0 and LIVE_RUN_PROMISE not in printed
    assert "it waits for the card's next run" in printed


def test_the_comment_tool_on_an_exited_worker_claims_no_live_run(board, monkeypatch):
    conn, context = board
    monkeypatch.setattr(kt, '_persisted_identity', lambda: 'default')
    _exited_continued_run(conn, context)
    out = json.loads(kt._handle_comment({'task_id': context['task_id'], 'body': 'are you there'}))
    assert out['live_run'] is False and out['worker_exited'] is True
    assert out['delivery'] == EXITED_NOTE and LIVE_RUN_PROMISE not in out['delivery']


def test_a_live_worker_keeps_the_live_run_promise(board, monkeypatch):
    conn, context = board
    monkeypatch.setattr(kt, '_persisted_identity', lambda: 'default')
    dispatch._set_worker_pid(conn, context['task_id'], os.getpid())  # a live outer, nothing booked yet
    assert not worker_exited(conn, context['task_id'], context['run_id'])
    out = json.loads(kt._handle_comment({'task_id': context['task_id'], 'body': 'hello worker'}))
    assert out['live_run'] is True and LIVE_RUN_PROMISE in out['delivery'] and 'worker_exited' not in out


def test_a_checkpointed_turn_with_a_live_submitter_is_still_live(board):
    """The restart gap: the old owner checkpointed the turn and the submitter waits for the next owner."""
    conn, context = board
    shapes._failed_child(conn, context, outer_pid=os.getpid())
    shapes.book(context, True)
    assert not worker_exited(conn, context['task_id'], context['run_id'])
