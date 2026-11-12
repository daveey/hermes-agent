"""Independent review probe: malformed synced access declarations fail closed."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_coordination_placement as fleet_helpers
from tests.hermes_cli import test_kanban_worker_policy_db as db_helpers

fleet = fleet_helpers.fleet
conn = db_helpers.conn


@pytest.fixture(autouse=True)
def _board_maps_access(monkeypatch):
    """These contracts only matter once the Board places declared access (core keeps it home until then)."""
    from hermes_cli import kanban_placement_coordination as coordination
    monkeypatch.setattr(coordination, "BOARD_PLACEABLE_ACCESS", frozenset(coordination.ACCESS_LABELS))


@pytest.mark.parametrize('stored', ['["hub:read"', '"hub:read"', '42', '[{}]',
                                     '{}', 'null', 'false', '[null]', '[[]]'])
def test_malformed_remote_contract_refused(conn, fleet, tmp_path, monkeypatch, stored):
    cfg, entry, _ = fleet
    entry['labels'].append('capability:hub-read')
    monkeypatch.setattr(kp, 'config', lambda: cfg)
    tid = kb.create_task(conn, title='Check remote evidence', body='read logs on the hub',
                         assignee='default', requires=['terminal', 'hub:read'])
    captured = conn.execute("SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? ORDER BY rowid LIMIT 1", (tid,)).fetchone()
    fields = json.loads(captured['fields'])
    fields.update(runs_on=json.dumps(['machine:worker']), created_on='hub', requires=stored)
    change = sync.RemoteChange(1, 'tasks', tid, 'upsert', fields, 1, '', 'hub')
    with kbc.connect(tmp_path / 'replica.db') as replica, kbc.connect(tmp_path / 'worker.db') as worker:
        client.ensure_identity(replica, 'default', 'worker')
        client.ensure_identity(worker, 'default', 'worker')
        sync.set_mode(worker, 'primary')
        sync.apply_remote_changes(replica, [change], new_cursor=1, observers=False)
        assert node.import_cards(worker, replica, [tid]) == [tid]
        row = worker.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        entry['labels'].remove('capability:hub-read')
        assert wp.execution_refusal(worker, row) == 'invalid worker access requirements'
        from hermes_cli import kanban_placement_coordination as coordination
        assert coordination.claim_refusal(dict(row), 'worker') == 'invalid worker access requirements'


@pytest.mark.parametrize('stored', [None, '', '[]', [], '["terminal"]', ['terminal']])
def test_empty_and_legacy_contracts_remain_valid(stored):
    from hermes_cli import kanban_placement_coordination as coordination
    card = dict(placement_policy='worker-first-v1', title='Plain task', requires=stored)
    assert coordination.claim_refusal(card, 'worker') is None


def test_unenrolled_legacy_card_remains_outside_gate():
    from hermes_cli import kanban_placement_coordination as coordination
    assert coordination.claim_refusal(dict(requires='broken'), 'worker') is None

