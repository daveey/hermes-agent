"""Real capture/outbox/HLC/sync/replica round trip with the in-memory Board transport."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_model_retirement as retirement
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    svc = FakeService()
    path = home / 'kanban.db'
    with kbc.connect(path) as conn:
        client.ensure_identity(conn, 'default', 'owner')
        sync.set_mode(conn, 'primary')
        yield conn, path, svc


def roundtrip(path, svc):
    result = client.sync_board('default', path, svc.hub('owner'), 'owner')
    assert result.error is None
    assert result.push is None or result.push.rejected == 0


def test_policy_field_clocks_and_replica_readback(live):
    conn, path, svc = live
    tid = kb.create_task(conn, title='ordinary work', body='scope', assignee='default')
    conn.execute("UPDATE tasks SET model_override='grok-4.6',provider_override='xai-oauth',model_pin_mandatory=1,body='scope\r\nrequire_model: true\r\n' WHERE id=?", (tid,))
    roundtrip(path, svc)
    roundtrip(path, svc)
    before = dict(svc.board('default')['rows']['tasks'][tid]['field_hlc'])
    assert retirement.sweep(conn) == 1
    roundtrip(path, svc)
    roundtrip(path, svc)
    stored = svc.board('default')['rows']['tasks'][tid]
    for key in retirement.FIELDS:
        assert stored[key] is None
        assert stored['field_hlc'][key] > before[key]
    assert stored['field_hlc']['body'] > before['body']
    # Read the actual shadow SQLite replica, not just the fake transport reply.
    with kbc.connect(client.shadow_path('default')) as replica:
        row = replica.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert all(row[key] is None for key in retirement.FIELDS)
        assert row['body'] == 'scope\r\n' + retirement.PREFIX + json.dumps('require_model: true') + '\r\n'
        assert replica.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='model_override_cleared'", (tid,)).fetchone()[0] == 1
        assert replica.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND author='kanban-dispatcher'", (tid,)).fetchone()[0] == 2
    head = svc.board('default')['seq']
    assert retirement.sweep(conn) == 0
    roundtrip(path, svc)
    assert svc.board('default')['seq'] == head


def test_foreign_copy_and_changed_ownership_are_not_repaired(live):
    conn, _path, _svc = live
    tid = kb.create_task(conn, title='ordinary work', assignee='default')
    conn.execute("UPDATE tasks SET model_override='grok-4.6',provider_override='xai-oauth',model_pin_mandatory=1,body='require_model: true' WHERE id=?", (tid,))
    snapshot = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    # Simulate a foreign retained executor copy, including ownership changing after selection.
    conn.execute('UPDATE tasks SET created_on=? WHERE id=?', ('other-gateway', tid))
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert retirement.sweep(conn) == 0
    assert not retirement.retire_selectors(conn, snapshot)
    assert not retirement.retire_body(conn, snapshot)
    assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
