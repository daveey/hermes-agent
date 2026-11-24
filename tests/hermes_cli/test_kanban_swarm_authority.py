"""Swarm front door must not create an authority-less fan-out."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_create_authority import CreateAuthorityRefused, has_authority
from hermes_cli.kanban_swarm import create_swarm, SwarmWorkerSpec
from tests.hermes_cli.test_kanban_create_scope_authority import home as _home, _seed_session

home = _home


def _create(conn):
    return create_swarm(conn, goal='bounded research', workers=[SwarmWorkerSpec(profile='default', title='Research', body='bounded investigation')],
                        verifier_assignee='default', synthesizer_assignee='default', created_by='default')


def test_swarm_unknown_agent_session_rolls_back_entire_graph(home, monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_ID', 'unknown-agent')
    with kbc.connect_closing() as conn:
        with pytest.raises(CreateAuthorityRefused):
            _create(conn)
        assert not conn.execute('SELECT 1 FROM tasks').fetchone()


@pytest.mark.parametrize('registered', [False, True])
def test_decomposition_requires_durable_authority_atomically(home, monkeypatch, registered):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    chat = '20261123_110000_456def'
    if registered:
        _seed_session(home, chat)
    monkeypatch.setenv('HERMES_SESSION_ID', chat)
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='triage source', assignee='default', triage=True)
        args = dict(root_assignee='default', children=[{'title': 'bounded child', 'assignee': 'default'}])
        if not registered:
            with pytest.raises(CreateAuthorityRefused):
                decompose_triage_task(conn, root, **args)
            assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
            assert kb.get_task(conn, root).status == 'triage'
        else:
            children = decompose_triage_task(conn, root, **args)
            assert children and all(has_authority(conn, tid, None) for tid in children)


def test_swarm_keeps_registered_chat_authority_after_root_finishes(home, monkeypatch):
    chat = '20261123_110000_123abc'
    _seed_session(home, chat)
    monkeypatch.setenv('HERMES_SESSION_ID', chat)
    with kbc.connect_closing() as conn:
        result = _create(conn)
        assert kb.get_task(conn, result.root_id).status == 'done'
        for tid in [*result.worker_ids, result.verifier_id, result.synthesizer_id]:
            assert has_authority(conn, tid, None)
            assert conn.execute('SELECT session_id FROM tasks WHERE id=?', (tid,)).fetchone()[0] == chat
