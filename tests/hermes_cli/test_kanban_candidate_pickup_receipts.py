from unittest.mock import Mock
import pytest
from hermes_cli import kanban_candidate_receipts as receipts
from hermes_cli import kanban_worker_node as node


@pytest.mark.parametrize('status', ['ready', 'review'])
@pytest.mark.parametrize('stage', ['prefilter', 'import'])
def test_pickup_refusal_preserves_lane(monkeypatch, status, stage):
    from hermes_cli import kanban_hub_admission_pickup as admission
    from hermes_cli import kanban_pickup_held as held
    from hermes_cli import kanban_replica_lag as lag
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_dispatch_priority as priority
    from hermes_cli import kanban_class_walls as walls
    # Receipt-only replay uses a Mock replica; real class admission has its own integration tests.
    monkeypatch.setattr(walls, 'pickup_refusal', lambda *a: None)
    card = dict(id='review-card', status=status, assignee='missing', runs_on=['pool:worker'])
    monkeypatch.setattr(node, 'pickup_mode', lambda: 'pool')
    monkeypatch.setattr(node.sync, 'board_mode', lambda _: 'primary')
    for name in ('evict', '_refill_copies', '_record_skips'):
        monkeypatch.setattr(node, name, lambda *a: None)
    monkeypatch.setattr(node, 'open_replica', lambda _: Mock())
    monkeypatch.setattr(node, '_own', lambda _: 'worker-a1')
    monkeypatch.setattr(node, '_replica_path', lambda _: 'unused')
    monkeypatch.setattr(node, '_unimported_pinned', lambda *a: {'review-card'})
    monkeypatch.setattr(node, 'registry', lambda: dict(machines={}))
    monkeypatch.setattr(node, 'candidates', lambda *a: [card])
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    monkeypatch.setattr(priority, 'order_with_remote_waiters', lambda conn, rows: rows)
    monkeypatch.setattr(node, '_skip_reason', lambda *a, **kw:
                        ('profile_ineligible', 'missing profile') if stage == 'prefilter' else None)
    for name in ('_quota_held', '_review_walled', '_paced'):
        monkeypatch.setattr(node, name, lambda *a: None)
    monkeypatch.setattr(held, 'board_hold', lambda *a: None)
    monkeypatch.setattr(lag, 'lagging', lambda *a: [])
    monkeypatch.setattr(admission, 'screen', lambda *a: (a[-1], {}, {}))
    monkeypatch.setattr(admission, 'apply', lambda *a: {})
    monkeypatch.setattr(node, 'import_cards', lambda *a: [])
    monkeypatch.setattr(node, 'import_refusals', dict)
    monkeypatch.setattr(held, 'remember', lambda *a: None)
    with receipts.collect(worker='worker-a1') as capture:
        assert node.pickup(Mock(), budget=1) == []
    import sqlite3
    conn = sqlite3.connect(':memory:')
    receipts.persist(conn, capture)
    exported = receipts.read(conn)
    rows = exported['receipts']
    assert len(rows) == (1 if status == 'ready' else 0), rows
    if rows:
        assert rows[0]['reason'] == ('profile_ineligible: missing profile' if stage == 'prefilter'
                                     else 'other: import failed')
