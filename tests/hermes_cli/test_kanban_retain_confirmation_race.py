"""Concurrent receipt recovery cannot resurrect rejected first-wins custody."""
import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_failure_release as release
from hermes_cli import kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_failure_transport as support
from tests.hermes_cli import test_kanban_retain_receipt as receipts
from tests.hermes_cli.test_kanban_retain_transport import reserve

live = support.live


@pytest.mark.parametrize('fallback', [True, False])
def test_confirmation_cannot_overwrite_concurrent_definitive_rejection(live, fallback):
    owned, _receipt, _rows, hub = receipts.fixture(live)
    original = hub.ops
    def raced(board, ops):
        result = original(board, ops)
        with kbc.write_txn(live):
            transport.record_result(live, {'op_id': owned['op_id']}, {'status': 'rejected', 'reason': 'episode_changed'})
            if fallback:
                rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
                release.queue(live, owned['task_id'], rid, outcome='crashed', error='isolated')
        return result
    hub.ops = raced
    with pytest.raises(ValueError, match='custody'):
        transport.confirm(live, hub, 'default', owned['run_uid'])
    after = transport.command(live, owned['run_uid'])
    assert after['delivery_state'] == 'rejected'
    assert after['canonical_receipt_json'] is None
    assert bool(after['rejected_release_op_id']) is fallback


def test_rejected_retain_keeps_source_launch_fence(live):
    from hermes_cli import kanban_source_launch as launch
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    with kbc.write_txn(live):
        transport.record_result(live, {'op_id': owned['op_id']}, {'status': 'rejected', 'reason': 'episode_changed'})
    assert launch.release_with_receipt(live, tid, rid, {}, outcome='crashed', error='isolated') == 'taken'
    assert release.command_owned(live, rid) and not release.observer_held(live, rid)
    assert not release.queued(live, rid)
