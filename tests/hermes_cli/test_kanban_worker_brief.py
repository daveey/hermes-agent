"""Compact worker context keeps recent complete instructions within a fixed budget."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_context_parent_cap as helpers

kanban_home = helpers.kanban_home


def test_compact_context_last_five_comments_in_full(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='small card', body='Acceptance: exact delivery', assignee='worker')
        for i in range(8):
            kb.add_comment(conn, tid, 'worker', f'Comment {i}: ' + 'detail ' * 30)
        brief = kb.build_worker_context(conn, tid, compact=True)
        full = kb.build_worker_context(conn, tid)
        assert len(brief) <= 10_000
        assert 'Acceptance: exact delivery' in brief
        assert 'Comment 2:' not in brief and 'Comment 2:' in full
        for i in range(3, 8):
            assert (f'Comment {i}: ' + 'detail ' * 30).strip() in brief
        assert 'verbose=true' in brief


def test_oversized_current_instructions_require_full_read(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='large card', body='x' * 20_000, assignee='worker')
        brief = kb.build_worker_context(conn, tid, compact=True)
        assert len(brief) <= 10_000
        assert 'Do not execute' in brief
        assert 'verbose=true' in brief


@pytest.mark.parametrize('marker', ['HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK'])
def test_worker_cli_show_uses_same_brief_and_can_expand(kanban_home, capsys, monkeypatch, marker):
    from argparse import Namespace
    from hermes_cli.kanban import _cmd_show
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='CLI card', body='current body', assignee='worker')
        for i in range(9):
            kb.add_comment(conn, tid, 'worker', f'CLI history {i}')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    monkeypatch.setenv(marker, tid)
    assert _cmd_show(Namespace(task_id=tid, json=False, verbose=False)) == 0
    text = capsys.readouterr().out
    assert 'current body' in text and 'verbose=true' in text
    assert len(text) <= 10_001
    assert 'CLI history 0' not in text
    assert _cmd_show(Namespace(task_id=tid, json=True, verbose=True)) == 0
    assert 'CLI history 0' in capsys.readouterr().out


def test_overflow_preserves_governing_rules_when_they_fit(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='long', body='x' * 20_000, assignee='worker')
        kb.add_comment(conn, tid, 'operator', 'standing: Never change acceptance without approval.')
        text = kb.build_worker_context(conn, tid, compact=True)
        assert len(text) <= 10_000 and 'Do not execute' in text
        assert 'Never change acceptance without approval.' in text
