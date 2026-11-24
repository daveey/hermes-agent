"""Reviewer probe: real owner observation beneath explicit local binding."""
import json
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_local as local

@pytest.mark.parametrize('local_only_control', [False, True])
@pytest.mark.parametrize('status', ['ready', 'running', 'scheduled', 'blocked'])
def test_explicit_local_binding_never_discovers_shadow(tmp_path, monkeypatch, status, local_only_control):
    home = tmp_path / 'authorized'
    home.mkdir()
    with kbc.connect_closing(home / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='Bound observation', assignee='default', completion_contract='local-only')
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
        binding = {'binding_id': 'review-binding', 'version': 1,
            'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default', 'task_id': tid},
            'origin': {'gateway': 'owner', 'profile': 'default', 'session': 'chat'},
            'task_record_key': 'default/default/' + tid}
        conn.execute('CREATE TABLE origin_creation_intents (task_id TEXT, binding_json TEXT)')
        conn.execute('INSERT INTO origin_creation_intents VALUES (?, ?)', (tid, json.dumps(binding)))
    if local_only_control:
        from hermes_cli import kanban_session_activity_card_details as details
        original = details._add_blocked_upstream
        monkeypatch.setattr(details, '_add_blocked_upstream',
                            lambda conn, rows, **kw: original(conn, rows, replica=None))
    def forbidden(*a, **kw):
        pytest.fail('explicit owner observation reached ambient shadow_path')
    monkeypatch.setattr('hermes_cli.kanban_sync_client.shadow_path', forbidden)
    row = local.observe_local_binding(binding, profile='default', now=100, home=home)
    assert row is not None
    assert row['status'] == status
