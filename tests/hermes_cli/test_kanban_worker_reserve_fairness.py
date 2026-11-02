"""Bounded placement batches must rotate past unchanged Board refusals."""
import sqlite3

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}


@pytest.mark.parametrize("refused_count", [4, 9])
def test_persistent_refusals_cannot_starve_later_cards(conn, monkeypatch, refused_count):
    refused = [fixtures.acknowledged(conn, sync_version=5) for _ in range(refused_count)]
    fresh = fixtures.acknowledged(conn, sync_version=9)
    seen = []

    class Hub:
        def reserve(self, board, **body):
            seen.append(body["task_id"])
            if body["task_id"] in refused:
                raise client.HubError(409, "explicit_pin")
            return {"state": "assigned"}

    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    # Reopen each tick: fairness must survive connection/process churn, not a
    # module-global counter that different profiles can reset or share.
    ticks = (refused_count + 1 + wp.MAX_RESERVES_PER_TICK - 1) // wp.MAX_RESERVES_PER_TICK
    for _ in range(ticks):
        before = len(seen)
        with sqlite3.connect(path) as tick:
            tick.row_factory = sqlite3.Row
            wp.reevaluate(tick, Hub(), "default", "hub", cfg=CFG)
        tick.close()
        assert 0 < len(seen) - before <= 4
    assert fresh in seen
    assert set(refused).issubset(seen)
    # A full second circuit must retry all rows, including refused unchanged ones.
    seen.clear()
    for _ in range(ticks):
        before = len(seen)
        wp.reevaluate(conn, Hub(), "default", "hub", cfg=CFG)
        assert 0 < len(seen) - before <= 4
    assert set(seen) == {*refused, fresh}


def test_transport_stop_advances_only_the_attempted_card(conn, monkeypatch):
    tasks = [fixtures.acknowledged(conn) for _ in range(5)]
    seen = []

    class Hub:
        def reserve(self, board, **body):
            seen.append(body["task_id"])
            raise OSError("offline")

    monkeypatch.setattr(client, "pull", lambda *a, **kw: pytest.fail("transport stop must not pull"))
    for _ in tasks:
        before = len(seen)
        assert wp.reevaluate(conn, Hub(), "default", "hub", cfg=CFG) == 0
        assert len(seen) == before + 1
    assert set(seen) == set(tasks)
