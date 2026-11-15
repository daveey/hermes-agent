"""Boot recovery retains chronological spool custody on transient lineage reads."""
import sqlite3

import pytest

from gateway import shutdown_flush
from tests.gateway import test_parked_passive_row_delivery as helpers

parked = helpers.parked
spool_home = helpers.spool_home


@pytest.mark.parametrize('later_sources', [('s',), ('tip',), ('s', 'tip')])
def test_route_read_failure_preserves_order(parked, spool_home, monkeypatch, later_sources):
    db, _, row = parked
    assert shutdown_flush.defer_row_for_parked_turn('s', {'role': 'user', 'content': 'first'})
    helpers._resume(db, row)
    db.publish_compression_child(parent_session_id='s', child_session_id='tip', source='test',
                                 messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
    for index, sid in enumerate(later_sources):
        assert shutdown_flush.defer_row_for_parked_turn(sid, {'role': 'user', 'content': f'later {index}'})
    db.create_session('unrelated', source='test')
    assert shutdown_flush.defer_row_for_parked_turn('unrelated', {'role': 'user', 'content': 'independent'})
    before = db.get_messages('s')
    preserved = {p: p.read_bytes() for p in helpers._spool_files()}
    read = db._read_ctx
    calls = []

    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise sqlite3.OperationalError('transient lineage read failure')
        return read()

    with monkeypatch.context() as patcher:
        patcher.setattr(db, '_read_ctx', fail_once)
        recovered, blocked = shutdown_flush.recover_pending_spool(db)
    assert recovered == 1
    # The failed lookup only knew s; holding a later row must also mark its
    # destination so the live writer knows tip still has spool custody.
    assert blocked == {'s', 'tip'}
    assert [m['content'] for m in db.get_messages('tip')] == ['summary']
    assert [m['content'] for m in db.get_messages('unrelated')] == ['independent']
    remaining = helpers._spool_files()
    assert len(remaining) == 1 + len(later_sources)
    assert all(p.read_bytes() == preserved[p] for p in remaining)
    assert shutdown_flush.recover_pending_spool(db) == (1 + len(later_sources), set())
    assert shutdown_flush.recover_pending_spool(db) == (0, set())
    assert not helpers._spool_files()
    assert db.get_messages('s') == before
    assert [m['content'] for m in db.get_messages('tip')] == [
        'summary', 'first', *(f'later {i}' for i in range(len(later_sources)))]
    assert [m['content'] for m in db.get_messages('unrelated')] == ['independent']
