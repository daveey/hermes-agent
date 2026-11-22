"""Retain reply ordering must preserve ACK custody and genuine transient recovery."""
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_failure_release as release
from hermes_cli import kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_failure_transport as support
from tests.hermes_cli import test_kanban_retain_receipt as receipts

live = support.live


@pytest.mark.parametrize('boundary', ['result', 'confirm'])
@pytest.mark.parametrize('reply', [
    {'status': 'rejected', 'reason': 'episode_changed'},
    {'status': 'deferred'},
])
def test_acknowledged_command_cannot_be_downgraded(live, boundary, reply):
    owned, _receipt, _rows, hub = receipts.fixture(live)
    with kbc.write_txn(live):
        transport.record_result(live, {'op_id': owned['op_id']}, {'status': 'applied'})
    result = {'client_op_id': owned['request_uid'], **reply}
    if boundary == 'result':
        with kbc.write_txn(live):
            transport.record_result(live, {'op_id': owned['op_id']}, result)
    else:
        hub.ops = lambda *a: {'results': [result]}
        with pytest.raises(ValueError):
            transport.confirm(live, hub, 'default', owned['run_uid'])
    assert transport.command(live, owned['run_uid'])['delivery_state'] == 'acknowledged'
    rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
    assert release.observer_held(live, rid)
    assert not release.queued(live, rid)


@pytest.mark.parametrize('boundary', ['result', 'confirm'])
@pytest.mark.parametrize('reason,detail', [
    ('storage_error', ''), ('depends_on_rejected', ''),
    ('invalid', 'unknown table task_events'), ('not_found', 'no task yet'),
])
def test_transient_rejection_never_grants_failure_fallback(live, boundary, reason, detail):
    owned, _receipt, _rows, hub = receipts.fixture(live)
    result = {'client_op_id': owned['request_uid'], 'status': 'rejected', 'reason': reason, 'detail': detail}
    if boundary == 'result':
        with kbc.write_txn(live):
            assert transport.record_result(live, {'op_id': owned['op_id']}, result) == 'deferred'
    else:
        hub.ops = lambda *a: {'results': [result]}
        with pytest.raises(ValueError):
            transport.confirm(live, hub, 'default', owned['run_uid'])
    assert transport.command(live, owned['run_uid'])['delivery_state'] == 'unknown'
    rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
    assert release.observer_held(live, rid)
    assert not release.queued(live, rid)
    assert transport.outbound(live, dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='retain'").fetchone()))


def test_legacy_read_only_command_readers_do_not_migrate(live, monkeypatch):
    # The pre-retain table has no operation/delivery/fallback columns.
    tid, rid = support._running(live)
    monkeypatch.setattr(release.sync, 'run_uid', lambda *a: 'legacy-run')
    with kbc.write_txn(live):
        live.execute('CREATE TABLE kanban_failure_commands (run_uid TEXT PRIMARY KEY,task_id TEXT,op_id TEXT)')
        live.execute('INSERT INTO kanban_failure_commands VALUES (?,?,?)', ('legacy-run', tid, 'legacy-op'))
    path = live.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        assert release.command_owned(conn, rid)
        assert release.observer_held(conn, rid)
        assert release.queued(conn, rid)
        assert release.release_predicate(conn) == '1'
        assert [r[1] for r in conn.execute('PRAGMA table_info(kanban_failure_commands)')] == ['run_uid', 'task_id', 'op_id']
