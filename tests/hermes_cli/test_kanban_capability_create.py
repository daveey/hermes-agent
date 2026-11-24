"""Assignment preflight belongs at the shared mutation boundary."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_capabilities import CapabilityMismatchError


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    profile = home / 'profiles' / 'restricted'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('platform_toolsets:\n  cli: [web]\nterminal:\n  backend: local\n')
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn


def test_shared_create_rejects_incompatible_worktree(board, tmp_path):
    with pytest.raises(CapabilityMismatchError) as err:
        kb.create_task(board, title='Repo work', assignee='restricted',
                       workspace_kind='worktree', workspace_path=str(tmp_path))
    assert err.value.code == 'assignee_capability_mismatch'
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_explicit_requirements_are_validated_and_persisted(board):
    with pytest.raises(CapabilityMismatchError):
        kb.create_task(board, title='Shell', assignee='restricted', requires=['terminal'])
    tid = kb.create_task(board, title='Unconstrained', assignee='restricted', requires=[])
    assert kb.get_task(board, tid).requires == []


def test_decomposition_cannot_bypass_assignment_preflight(board):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    root = kb.create_task(board, title='Parent', assignee='restricted', triage=True)
    with pytest.raises(CapabilityMismatchError):
        decompose_triage_task(board, root, root_assignee='restricted', children=[
            {'title': 'Safe', 'assignee': 'restricted'},
            {'title': 'Shell', 'assignee': 'restricted', 'requires': ['terminal']},
        ])
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_unassigned_draft_can_be_created_but_assignment_must_validate(board, tmp_path):
    tid = kb.create_task(board, title='Draft repo work', workspace_kind='dir',
                         workspace_path=str(tmp_path))
    assert kb.get_task(board, tid).requires == ['terminal', 'file', 'host_repo']
    with pytest.raises(CapabilityMismatchError):
        kb.assign_task(board, tid, 'restricted')
    assert kb.get_task(board, tid).assignee is None


def test_decomposer_keeps_requirements_for_shared_validation(board):
    from hermes_cli.kanban_decompose import _Routing, _clean_children
    from hermes_cli.kanban_db_graph import decompose_triage_task
    routing = _Routing('restricted', 'restricted', True, [], {'restricted'})
    children, error = _clean_children('root', [
        {'title': 'Shell', 'assignee': 'restricted', 'requires': ['terminal']},
    ], routing)
    assert not error
    root = kb.create_task(board, title='Parent', assignee='restricted', triage=True)
    with pytest.raises(CapabilityMismatchError):
        decompose_triage_task(board, root, root_assignee='restricted', children=children)
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('surface', ['tool', 'cli'])
def test_agent_create_defaults_to_creator_and_checks_requires(board, monkeypatch, surface):
    import argparse
    import json
    from hermes_cli import kanban as cli
    from tools import kanban_tools as tools
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'restricted')
    monkeypatch.setattr(cli, '_profile_author', lambda: 'restricted')
    monkeypatch.setattr(cli, '_agent_invoked', lambda: True)
    monkeypatch.setattr(cli, '_check_dispatcher_presence', lambda: (True, ''))
    from tests.hermes_cli.test_kanban_selfwork_entry import register_chat
    monkeypatch.setenv('HERMES_SESSION_ID', register_chat(kb.kanban_home(), '20261123_120000_capabilities'))
    if surface == 'tool':
        out = json.loads(tools._handle_create({'title': 'Defaulted'}))
        assert out.get('task_id'), out
        tid = out['task_id']
        error = json.loads(tools._handle_create({'title': 'Needs shell', 'requires': ['terminal']}))
        assert error['code'] == 'assignee_capability_mismatch'
    else:
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers())
        assert cli._cmd_create(parser.parse_args(['kanban', 'create', 'Defaulted'])) == 0
        tid = board.execute("SELECT id FROM tasks WHERE title='Defaulted'").fetchone()[0]
        assert cli._cmd_create(parser.parse_args(['kanban', 'create', 'Needs shell',
                                                  '--requires', 'terminal'])) == 2
    assert kb.get_task(board, tid).assignee == 'restricted'
