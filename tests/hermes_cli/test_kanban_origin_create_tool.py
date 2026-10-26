"""Actual registered tool -> atomic owner intent, isolated by canonical test runner."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_state import SessionDB
from tools import kanban_tools  # normal built-in registration
from tools.registry import registry


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_ORIGIN_SESSION'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('creator', 'cli')
    db.close()
    kb.init_db()
    return {'request_id': 'request-tool-001',
            'owner': {'gateway': 'worker', 'profile': 'default', 'board': 'default'},
            'origin': {'gateway': 'origin', 'profile': 'work', 'session': 's_old'}}


def create(intent, **changes):
    args = {'title': 'Build', 'assignee': 'default', 'session_id': 'creator', **changes}
    return json.loads(registry.dispatch('kanban_create', args, origin_intent=intent))


def test_registered_tool_persists_intent_and_returns_pending_handoff(world):
    result = create(world)
    assert result.get('created') is True, result
    assert result['ok'] is False, result
    assert result['code'] == 'origin_bind_pending'
    assert result.get('handoff_complete') is False, result
    assert result['origin_creation']['request_id'] == world['request_id']
    with kbc.connect_closing() as conn:
        row = conn.execute('SELECT * FROM origin_creation_intents').fetchone()
        assert result['task_id'] == row['task_id']
        assert result['origin_creation']['binding'] == json.loads(row['binding_json'])
        assert result['origin_creation']['child_request_id'] == row['child_request_id']
        assert kb.get_task(conn, result['task_id']).session_id == 'creator'
    again = create(world)
    assert again['task_id'] == result['task_id']
    assert again['origin_creation'] == result['origin_creation']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_retry_ignores_ambient_changes_and_retains_creator(world, monkeypatch):
    first = create(world)
    monkeypatch.setenv('HERMES_TENANT', 'changed-ambient-tenant')
    db = SessionDB()
    db.delete_session('creator')
    db.close()
    retry = create(world)
    assert retry.get('task_id') == first['task_id'], retry
    assert retry['origin_creation'] == first['origin_creation']
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, first['task_id']).session_id == 'creator'


def test_conflicting_retry_is_typed_and_does_not_duplicate(world):
    first = create(world)
    result = create(world, title='Changed')
    assert result.get('code') == 'origin_creation_conflict', result
    assert result['handoff_complete'] is False
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert kb.get_task(conn, first['task_id']).title == 'Build'


def test_archive_retry_retains_exact_marker(world):
    first = create(world)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (first['task_id'],))
    retry = create(world)
    assert retry['task_id'] == first['task_id']
    assert retry['origin_creation'] == first['origin_creation']
    assert retry['handoff_complete'] is False


def test_llm_cannot_supply_internal_creation_context(world):
    result = json.loads(registry.dispatch('kanban_create', {
        'title': 'Build', 'assignee': 'default', 'origin_intent': world}))
    assert 'unknown parameter' in result['error']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
