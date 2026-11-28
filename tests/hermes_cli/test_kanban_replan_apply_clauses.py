"""Each clause of the replan publication helper has its own killing test.

These call ``kanban_replan_apply`` directly so no earlier ``auto_replan`` guard, replica fallback or
event de-duplication can mask a clause.
"""
from contextlib import contextmanager
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replan_apply as publication
from tests.hermes_cli import test_kanban_auto_replan as helpers

kanban_home = helpers.kanban_home

AUTHOR = 'kanban-auto-replan'


@pytest.fixture(autouse=True)
def no_replica(monkeypatch):
    """Direct apply with a missing plan must not depend on whether a replica file exists."""
    from hermes_cli import kanban_foreign_card as foreign

    @contextmanager
    def none(*a, **kw):
        yield None
    monkeypatch.setattr(foreign, 'open_replica', none)


def _task(conn, title='Original', body='Original acceptance.'):
    return kb.create_task(conn, title=title, body=body, assignee='default')


def _parent(conn, *, status='done', result=None, summary=None, title='Replan parent'):
    """A parent whose status, ``tasks.result`` and run summary are set independently."""
    pid = _task(conn, title=title, body='parent body')
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=?, result=? WHERE id=?', (status, result, pid))
        if summary is not None:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
                "VALUES (?, 'default', 'done', 1000, 1001, 'completed', ?)", (pid, summary))
    return pid


def _apply(conn, tid, parent):
    with kb.write_txn(conn):
        return publication.apply(conn, tid, parent)


def _events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == publication.APPLIED]


def _comments(conn, tid):
    return [c for c in kb.list_comments(conn, tid) if c.author == AUTHOR]


def _unchanged(conn, tid, body, ncomments=0, nevents=0):
    assert kb.get_task(conn, tid).body == body
    assert len(_comments(conn, tid)) == ncomments
    assert len(_events(conn, tid)) == nevents


# -- published(): C143 (apply short-circuit), C148 (parent equality) ---------------------------------------

def test_repeated_direct_apply_is_idempotent(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='Plan one')
        assert _apply(conn, tid, parent) is True
        after = kb.get_task(conn, tid).body
        assert after == 'Original acceptance.\n\n## Replan handoff from ' + parent + '\nPlan one'
        assert _apply(conn, tid, parent) is True
        _unchanged(conn, tid, after, ncomments=1, nevents=1)
        assert publication.published(conn, tid, parent)


def test_published_receipt_returns_true_even_after_plan_disappears(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='Plan one')
        assert _apply(conn, tid, parent) is True
        body = kb.get_task(conn, tid).body
        assert kb.delete_task(conn, parent)
        assert publication.read_plan(conn, parent) is None
        # Only the short-circuit on the receipt can answer True once the plan is gone.
        assert _apply(conn, tid, parent) is True
        _unchanged(conn, tid, body, ncomments=1, nevents=1)


def test_receipt_for_another_parent_does_not_count_as_published(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        first = _parent(conn, result='First plan', title='First')
        second = _parent(conn, result='Second plan', title='Second')
        assert _apply(conn, tid, first) is True
        assert publication.published(conn, tid, first)
        assert not publication.published(conn, tid, second)
        assert _apply(conn, tid, second) is True
        body = kb.get_task(conn, tid).body
        assert 'First plan' in body and 'Second plan' in body
        assert len(_events(conn, tid)) == 2
        assert len(_comments(conn, tid)) == 2
        assert publication.published(conn, tid, second)


def test_published_ignores_other_event_kinds_and_other_cards(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        other = _task(conn, title='Other card')
        parent = _parent(conn, result='Plan one')
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'unrelated_kind', {'parent': parent})
            kb._append_event(conn, other, publication.APPLIED, {'parent': parent})
        assert not publication.published(conn, tid, parent)
        assert _apply(conn, tid, parent) is True
        assert len(_events(conn, tid)) == 1
        payload = json.loads(conn.execute(
            'SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, publication.APPLIED)).fetchone()[0])
        assert payload == {'parent': parent, 'mode': 'append_only_handoff'}


# -- plan source: C151 (status), C152 (summary), C153 (result) ---------------------------------------------

@pytest.mark.parametrize('status', ['todo', 'ready', 'running', 'blocked', 'review'])
def test_unfinished_parent_never_supplies_a_plan(kanban_home, status):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, status=status, result='Premature result', summary='Premature summary')
        assert publication._plan(conn, parent) is None
        assert publication.read_plan(conn, parent) is None
        assert _apply(conn, tid, parent) is False
        _unchanged(conn, tid, 'Original acceptance.')


def test_missing_parent_supplies_no_plan(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        assert publication._plan(conn, 'no-such-parent') is None
        assert _apply(conn, tid, 'no-such-parent') is False
        _unchanged(conn, tid, 'Original acceptance.')


def test_run_summary_is_the_plan_when_result_is_empty(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result=None, summary='Summary-only plan')
        assert publication._plan(conn, parent) == 'Summary-only plan'
        assert _apply(conn, tid, parent) is True
        assert kb.get_task(conn, tid).body.endswith('\nSummary-only plan')


def test_result_is_the_plan_when_no_run_summary_exists(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='Result-only plan', summary=None)
        assert kb.latest_summary(conn, parent) is None
        assert publication._plan(conn, parent) == 'Result-only plan'
        assert _apply(conn, tid, parent) is True
        assert kb.get_task(conn, tid).body.endswith('\nResult-only plan')


def test_run_summary_outranks_result(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='Stale result', summary='Newest summary')
        assert publication._plan(conn, parent) == 'Newest summary'
        assert _apply(conn, tid, parent) is True
        body = kb.get_task(conn, tid).body
        assert 'Newest summary' in body and 'Stale result' not in body


# -- missing / blank plan: C145, C155, C156 ----------------------------------------------------------------

def test_done_parent_without_any_plan_is_not_published(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result=None, summary=None)
        assert publication.read_plan(conn, parent) is None
        # A None plan must return False, not reach ``plan.strip()``.
        assert _apply(conn, tid, parent) is False
        _unchanged(conn, tid, 'Original acceptance.')


@pytest.mark.parametrize('blank', ['', ' ', '   ', '\n', '\t \n  ', '\r\n'])
def test_blank_result_is_not_published(kanban_home, blank):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result=blank, summary=None)
        assert _apply(conn, tid, parent) is False
        _unchanged(conn, tid, 'Original acceptance.')


@pytest.mark.parametrize('blank', [' ', '   ', '\n\t '])
def test_whitespace_only_summary_is_not_published(kanban_home, blank):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result=None, summary=blank)
        # latest_summary returns the whitespace string (non-empty), so only the strip clause rejects it.
        assert kb.latest_summary(conn, parent) == blank
        assert _apply(conn, tid, parent) is False
        _unchanged(conn, tid, 'Original acceptance.')


def test_blank_plan_leaves_card_publishable_once_a_real_plan_exists(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='  ')
        assert _apply(conn, tid, parent) is False
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET result=? WHERE id=?', ('Real plan', parent))
        assert _apply(conn, tid, parent) is True
        assert len(_events(conn, tid)) == 1


def test_plan_is_stripped_in_comment_and_body(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        parent = _parent(conn, result='\n  Padded plan  \n\n')
        assert _apply(conn, tid, parent) is True
        text = f'## Replan handoff from {parent}\nPadded plan'
        assert kb.get_task(conn, tid).body == 'Original acceptance.\n\n' + text
        comment, = _comments(conn, tid)
        assert comment.body == text


# -- original card missing: C147 -----------------------------------------------------------------------------

def test_missing_original_card_is_not_published(kanban_home):
    with kbc.connect_closing() as conn:
        parent = _parent(conn, result='A perfectly good plan')
        before = conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
        comments = conn.execute('SELECT COUNT(*) FROM task_comments').fetchone()[0]
        assert _apply(conn, 'no-such-original', parent) is False
        assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == before
        assert conn.execute('SELECT COUNT(*) FROM task_comments').fetchone()[0] == comments
        assert kb.get_task(conn, 'no-such-original') is None


# -- body fallback: C166/C167 --------------------------------------------------------------------------------

@pytest.mark.parametrize('stored', [None, ''])
def test_empty_original_body_is_treated_as_empty_text(kanban_home, stored):
    with kbc.connect_closing() as conn:
        tid = _task(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET body=? WHERE id=?', (stored, tid))
        assert not kb.get_task(conn, tid).body
        parent = _parent(conn, result='Plan for an empty card')
        assert _apply(conn, tid, parent) is True
        text = f'## Replan handoff from {parent}\nPlan for an empty card'
        assert kb.get_task(conn, tid).body == '\n\n' + text
        comment, = _comments(conn, tid)
        assert comment.body == text
        assert len(_events(conn, tid)) == 1


def test_nonempty_original_body_is_preserved_verbatim(kanban_home):
    with kbc.connect_closing() as conn:
        original = '  Keep acceptance line 1.\n- budget: 3\n'
        tid = _task(conn, body=original)
        parent = _parent(conn, result='Plan')
        assert _apply(conn, tid, parent) is True
        assert kb.get_task(conn, tid).body == original + '\n\n## Replan handoff from ' + parent + '\nPlan'
