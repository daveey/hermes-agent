"""A backlog of legacy ineligible cards must not consume the reservation lane."""
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures
from tests.hermes_cli import test_kanban_worker_reserve_client as reserve

conn = fixtures.conn
setup = fixtures.setup


def test_legacy_scan_does_not_starve_policy_renewal(setup):
    db, hub = setup
    for _ in range(wp.MAX_RESERVES_PER_TICK):
        fixtures.make(db, requires='["host_repo"]')
    tid = reserve.acknowledged(db)
    calls = []
    hub.reserve = lambda board, **body: calls.append(body) or {"state": "assigned", "machine": "worker-2"}
    wp.reevaluate(db, hub, "default", "hub")
    assert [call["task_id"] for call in calls] == [tid]


def test_legacy_scan_rotates_past_ineligible_rows(setup):
    db, hub = setup
    for _ in range(wp.MAX_RESERVES_PER_TICK):
        fixtures.make(db, requires='["host_repo"]')
    tid = fixtures.make(db)
    for _ in range(3):
        wp.reevaluate(db, hub, "default", "hub")
    assert tid in [call[1]["task_id"] for call in hub.calls]
