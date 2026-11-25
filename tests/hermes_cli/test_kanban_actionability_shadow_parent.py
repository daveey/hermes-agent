"""Replica lifecycle status satisfies only missing-live dependency edges."""
from contextlib import closing
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_actionability_sources import Reader, describe


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_TASK'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sync, 'sync_config', lambda: {'enabled': True, 'boards': ['default']})
    kb.init_db()
    shadow = home / 'kanban' / sync.SHADOW_DIR / 'default.db'
    shadow.parent.mkdir(parents=True, exist_ok=True)
    with closing(kbc.connect()) as live, closing(kbc.connect(shadow)) as replica:
        child = kb.create_task(live, title='executing reviewed work', assignee='default')
        assert kb.claim_task(live, child) is not None
        parent = kb.create_task(replica, title='remote review', assignee='default')
        replica.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        replica.commit()
        # The production shape: completed foreign review absent locally, edge retained.
        live.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)', (parent, child))
        sync.set_mode(live, 'shadow')
        live.commit()
        yield live, replica, child, parent


@pytest.mark.parametrize('mode', ['shadow', 'primary'])
@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_replica_parent_satisfies_running_dependency(boards, mode, status):
    live, replica, child, parent = boards
    live.execute('UPDATE kanban_sync_state SET mode=?', (mode,))
    replica.execute('UPDATE tasks SET status=? WHERE id=?', (status, parent))
    replica.commit()
    before = (live.total_changes, replica.total_changes)
    row = describe(live, child)
    assert row['dependencies'] == []
    assert row['action'] == 'execute_current_scope'
    assert row['escalation']['state'] == 'not_required'
    assert 'escalation_deadline_missing' not in row['violations']
    assert Reader(live).task(parent) is None  # no general task/evidence fallback
    assert before == (live.total_changes, replica.total_changes)


@pytest.mark.parametrize('status', ['running', 'ready', 'todo', 'triage', None])
def test_nonterminal_or_absent_replica_parent_remains_unknown(boards, status):
    live, replica, child, parent = boards
    if status is None:
        replica.execute('DELETE FROM tasks WHERE id=?', (parent,))
    else:
        replica.execute('UPDATE tasks SET status=? WHERE id=?', (status, parent))
    replica.commit()
    row = describe(live, child)
    assert row['action'] == 'satisfy_dependency'
    assert row['dependencies'][0]['status'] is None
    assert 'escalation_deadline_missing' in row['violations']


def test_shadow_stop_history_never_leaks_into_live_child(boards):
    live, replica, child, parent = boards
    payload = json.dumps({'disposition': {'reason': 'scope_stopped'}, 'episode_uid': 'shadow-stop'})
    for tid in (parent, child):
        replica.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'decision_retained',?,1)",
                        (tid, payload))
    replica.commit()
    row = describe(live, child)
    assert row['action'] == 'execute_current_scope'
    assert row['dependencies'] == []
    assert 'private_hold' not in row['violations']
    assert Reader(live).events(parent) == []


def test_live_parent_wins_over_terminal_replica(boards):
    live, _replica, child, parent = boards
    live.execute("INSERT INTO tasks(id,title,status,assignee,created_at) VALUES (?,'live parent','running','default',1)",
                 (parent,))
    row = describe(live, child)
    assert row['action'] == 'satisfy_dependency'
    assert row['dependencies'][0]['status'] == 'running'


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'disabled'])
def test_unavailable_replica_stays_unknown(boards, monkeypatch, failure):
    live, replica, child, _parent = boards
    if failure == 'missing':
        from hermes_cli import kanban_foreign_card
        monkeypatch.setattr(kanban_foreign_card, 'replica_path', lambda _: None)
    elif failure == 'malformed':
        replica.execute('ALTER TABLE tasks RENAME TO old_tasks')
        replica.commit()
    else:
        monkeypatch.setattr(sync, 'sync_config', lambda: {'enabled': False})
    row = describe(live, child)
    assert row['action'] == 'satisfy_dependency'
    assert row['dependencies'][0]['status'] is None


def test_live_child_stop_is_not_waived_by_terminal_replica(boards):
    live, _replica, child, _parent = boards
    payload = json.dumps({'disposition': {'reason': 'scope_stopped'}, 'episode_uid': 'live-stop'})
    live.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'decision_retained',?,9999999999)",
                 (child, payload))
    row = describe(live, child)
    assert row['action'] != 'execute_current_scope'
    assert row['blocker'] is not None
    assert row['escalation']['state'] != 'not_required'


def test_sync_off_does_not_use_replica(boards):
    live, _replica, child, _parent = boards
    live.execute("UPDATE kanban_sync_state SET mode='off'")
    row = describe(live, child)
    assert row['dependencies'][0]['status'] is None
    assert row['action'] == 'satisfy_dependency'
