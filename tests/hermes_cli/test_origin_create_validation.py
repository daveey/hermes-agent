"""Definitive pre-insert refusals must not hide behind write uncertainty."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_origin_owner as helpers

world = helpers.world


def _counts():
    with kbc.connect_closing() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] if table in tables else 0
                for table in ('tasks', 'task_links', 'task_events', 'origin_creation_intents', 'kanban_notify_subs')}


def test_foreign_parent_is_terminal_without_partial_writes(world, monkeypatch, capsys):
    from hermes_cli.kanban_dependency_validation import missing_dependency_error

    world['args']['create']['parents'] = ['t_foreign_parent']
    frozen = json.dumps(world, sort_keys=True)
    before = _counts()
    for _ in range(2):
        result = helpers.invoke(world, monkeypatch, capsys)
        assert result.get('status') == 'rejected', result
        assert result['code'] == 'origin_parent_unknown'
        assert str(missing_dependency_error(['t_foreign_parent'])) in result['error']
        assert result['created'] is False and result['handoff_complete'] is False
        assert _counts() == before
        assert json.dumps(world, sort_keys=True) == frozen


def test_exact_committed_replay_precedes_current_parent_validation(world, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title='Parent', settle_owner_chat=False)
    world['args']['create']['parents'] = [parent]
    first = helpers.invoke(world, monkeypatch, capsys)
    assert first.get('created') is True, first
    with kbc.connect_closing() as conn:
        conn.execute('DELETE FROM task_links WHERE parent_id=?', (parent,))
        conn.execute('DELETE FROM tasks WHERE id=?', (parent,))
        conn.commit()
    before = _counts()
    second = helpers.invoke(world, monkeypatch, capsys)
    assert second['origin_creation'] == first['origin_creation']
    assert _counts() == before
    assert before['tasks'] == 1


@pytest.mark.parametrize('failure', [ValueError, RuntimeError])
def test_postcommit_exception_is_pending_and_exact_retry_creates_once(world, monkeypatch, capsys, failure):
    original = kb.create_task

    def lost_reply(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('origin_intent') is not None:
            raise failure('reply lost after committed origin create')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(kb, 'create_task', lost_reply)
        result = helpers.invoke(world, monkeypatch, capsys)
    assert result == {'status': 'pending', 'code': 'origin_dispatch_pending', 'handoff_complete': False}
    before = _counts()
    assert before['tasks'] == before['origin_creation_intents'] == 1
    replay = helpers.invoke(world, monkeypatch, capsys)
    assert replay.get('created') is True, replay
    assert _counts() == before


def test_empty_parent_entry_still_creates_like_create_task(world, monkeypatch, capsys):
    """create_task drops falsy parents; the pre-insert check must not refuse them."""
    world['args']['create']['parents'] = ['']
    before = _counts()
    result = helpers.invoke(world, monkeypatch, capsys)
    assert result.get('created') is True, result
    after = _counts()
    assert after['tasks'] == before['tasks'] + 1
    assert after['task_links'] == before['task_links']
    assert world['args']['create']['parents'] == ['']


def test_mixed_empty_and_missing_parent_rejects_naming_only_missing(world, monkeypatch, capsys):
    from hermes_cli.kanban_dependency_validation import missing_dependency_error

    world['args']['create']['parents'] = ['', 't_missing_parent']
    before = _counts()
    result = helpers.invoke(world, monkeypatch, capsys)
    assert result.get('status') == 'rejected', result
    assert result['code'] == 'origin_parent_unknown'
    assert str(missing_dependency_error(['t_missing_parent'])) in result['error']
    assert str(missing_dependency_error(['', 't_missing_parent'])) not in result['error']
    assert _counts() == before
