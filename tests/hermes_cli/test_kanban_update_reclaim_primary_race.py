"""Primary-board observer must not enqueue an updater death for charged release."""
import pytest

from tests.hermes_cli import test_kanban_sync_leases as leases
from tests.hermes_cli import test_kanban_update_reclaim_race as race

kanban_home = leases.kanban_home
lease_client = leases.lease_client
synced_conn = leases.synced_conn
primary = leases.primary


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("observer_first", [True, False])
def test_primary_update_race(primary, lease_client, monkeypatch, observer_first, modern):
    race.test_update_reclaim_never_charges_or_clears_strikes(primary, monkeypatch, observer_first, modern)
    rows = primary.execute("SELECT name FROM sqlite_master WHERE name='kanban_failure_commands'").fetchall()
    if rows:
        assert primary.execute("SELECT COUNT(*) FROM kanban_failure_commands").fetchone()[0] == 0
