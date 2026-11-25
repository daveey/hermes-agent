"""Explicit source-bound helpers must not gate the card they assist."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_standing_parent_link import kanban_home
from tests.hermes_cli.test_kanban_actionability_dependencies import board, restrict

assert kanban_home and board


def test_declared_helper_cannot_become_source_prerequisite(kanban_home):
    with kbc.connect() as conn:
        source = kb.create_task(conn, title='source')
        helper = kb.create_task(conn, title='attachment recovery', creator_task_id=source,
                                body=f'<!-- kanban-helper-for: {source} -->\nRecover evidence only.')
        with pytest.raises(ValueError, match='helper_parent_forbidden.*creator_task_id'):
            kb.link_tasks(conn, helper, source)
        assert kb.parent_ids(conn, source) == []
        assert kb.get_task(conn, source).status == 'ready'


@pytest.mark.parametrize('entry', ['internal', 'create'])
def test_internal_and_creation_edges_cannot_evade_refusal(kanban_home, entry):
    from hermes_cli.kanban_dependency_validation import insert_link
    with kbc.connect() as conn:
        # Fixed valid source IDs cover synced boards as well as classic IDs.
        source = kb.create_task(conn, title='source')
        helper = kb.create_task(conn, title='helper', body=f'<!-- kanban-helper-for: {source} -->')
        before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        if entry == 'internal':
            with pytest.raises(ValueError, match='helper_parent_forbidden'):
                with kbc.write_txn(conn):
                    insert_link(conn, helper, source)
        else:
            # A declared helper can predate source creation (deterministic graph builders).
            conn.execute('UPDATE tasks SET body=? WHERE id=?',
                         ('<!-- kanban-helper-for: t_0123456789abcdef -->', helper))
            conn.commit()
            from unittest.mock import patch
            with patch.object(kb._sync, 'new_task_id', return_value='t_0123456789abcdef'):
                with pytest.raises(ValueError, match='helper_parent_forbidden'):
                    kb.create_task(conn, title='new source', parents=[helper])
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before
        assert kb.parent_ids(conn, source) == []


@pytest.mark.parametrize('kind', ['review', 'landing', 'escalation', 'helper title'])
def test_untyped_creator_children_remain_real_prerequisites(kanban_home, kind):
    with kbc.connect() as conn:
        source = kb.create_task(conn, title='source')
        parent = kb.create_task(conn, title=kind, creator_task_id=source)
        kb.link_tasks(conn, parent, source)
        assert kb.parent_ids(conn, source) == [parent]
        assert (parent, 'ready') in kb.parent_states(conn, source)


def test_declaration_only_refuses_its_exact_source_and_does_not_waive_existing_edges(kanban_home):
    from hermes_cli.kanban_dependency_validation import check_link_parent
    with kbc.connect() as conn:
        source = kb.create_task(conn, title='source')
        other = kb.create_task(conn, title='other')
        helper = kb.create_task(conn, title='helper')
        kb.link_tasks(conn, helper, source)
        # Classification after an old link must not clear it, even with archived status.
        conn.execute('UPDATE tasks SET body=?, status=? WHERE id=?',
                     (f'<!-- kanban-helper-for: {source} -->', 'archived', helper))
        conn.commit()
        with pytest.raises(ValueError, match='helper_parent_forbidden'):
            check_link_parent(conn, helper, source)
        assert kb.parent_ids(conn, source) == [helper]
        kb.link_tasks(conn, helper, other)
        assert kb.parent_ids(conn, other) == [helper]


def test_public_tool_returns_actionable_refusal(kanban_home):
    from tools.registry import registry
    import tools.kanban_tools
    assert tools.kanban_tools
    with kbc.connect() as conn:
        source = kb.create_task(conn, title='source')
        helper = kb.create_task(conn, title='helper', body=f'<!-- kanban-helper-for: {source} -->')
    tool = registry.get_entry('kanban_link')
    assert tool is not None
    result = json.loads(tool.handler({'parent_id': helper, 'child_id': source}))
    assert 'helper_parent_forbidden' in result['error']
    assert 'creator_task_id' in result['error']
    with kbc.connect() as conn:
        assert kb.parent_ids(conn, source) == []


def test_declaring_old_archived_helper_never_releases_restricted_edge(board):
    helper = kb.create_task(board, title='historical assistance')
    source = kb.create_task(board, title='source', parents=[helper])
    restrict(board, helper)
    assert kb.archive_task(board, helper)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET body=? WHERE id=?',
                      (f'<!-- kanban-helper-for: {source} -->', helper))
    kb.recompute_ready(board)
    assert kb.unsatisfied_parents(board, source) == [(helper, 'archived:restricted')]
    assert kb.get_task(board, source).status == 'todo'
    assert kb.claim_task(board, source) is None
    assert kb.parent_ids(board, source) == [helper]
