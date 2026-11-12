"""Adversarial boundary cases independent of valid-plan happy paths."""
import hashlib
import json

import pytest

from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_priority_cli as cli_helpers
from tests.hermes_cli import test_kanban_priority_executor as helpers
from tests.hermes_cli import test_kanban_priority_prepare as prepare_helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


@pytest.mark.parametrize('head', [True, 1.0])
def test_final_snapshot_requires_integer_head(head):
    from hermes_cli import kanban_priority_prepare as prepare

    class Malformed(prepare_helpers.Pages):
        def snapshot_page(self, *args, **kwargs):
            page = super().snapshot_page(*args, **kwargs)
            if kwargs.get('limit') == 1:
                page['head'] = head
            return page

    with pytest.raises(ValueError):
        prepare.capture(Malformed(), board_id='board', exceptions=[])


def test_executor_skips_unchanged_zero_rows(conn):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_priority_executor as executor
    ids, _, _ = helpers.setup_plan(conn)
    conn.execute('UPDATE tasks SET priority=0 WHERE id=?', (ids[0],))
    conn.commit()
    rows = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    manifest = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[])
    data = json.dumps(manifest).encode()
    attachment = kb.store_attachment_bytes(conn, ids[0], 'zero.json', data)
    conn.execute('DELETE FROM kanban_sync_outbox')
    conn.commit()
    result = executor.apply_attached(conn, attachment, hashlib.sha256(data).hexdigest(), writer=sync_helpers.ME)
    assert result['changed'] == 1
    changed = {row[0] for row in conn.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name='tasks'")}
    assert changed == {ids[2]}


def test_capture_refuses_out_of_order_valid_rows():
    from hermes_cli import kanban_priority_prepare as prepare

    class Unsorted:
        def snapshot_page(self, *args, **kwargs):
            rows = [{'id': key, 'priority': 1, 'created_on': 'one', 'status': 'ready'}
                    for key in ['b', 'a']]
            return {'table': 'tasks', 'head': 1, 'rows': rows, 'more': False}

    with pytest.raises(ValueError, match='cursor ordering'):
        prepare.capture(Unsorted(), board_id='board', exceptions=[])


@pytest.mark.parametrize('case', ['identity', 'local_writer', 'board'])
def test_verify_enrollment_refuses_before_network(conn, tmp_path, monkeypatch, case):
    from hermes_cli import kanban_priority_cli as cli
    from hermes_cli import kanban_sync_client as sync
    helpers.setup_plan(conn)
    rows = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    manifest = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[])
    path = tmp_path / 'original.json'
    data = json.dumps(manifest).encode()
    path.write_bytes(data)
    gateway = 'foreign' if case == 'identity' else sync_helpers.ME
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('https://example.test', 'fixture', gateway))

    def forbidden_hub(*args):
        pytest.fail('invalid identity reached canonical network')

    monkeypatch.setattr(sync, 'Hub', forbidden_hub)
    sql = {'local_writer': "UPDATE kanban_sync_state SET gateway_id='foreign'",
           'board': "UPDATE kanban_sync_state SET board_id='foreign'"}
    if case in sql:
        conn.execute(sql[case])
        conn.commit()
    namespace = cli_helpers.args('--priority-operation', 'verify', '--manifest', str(path),
                                 '--digest', hashlib.sha256(data).hexdigest(), '--writer', sync_helpers.ME)
    assert cli.run(conn, namespace) != 0
