"""Pin the inclusive, child-local link-event boundary requested in PR #2571."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_resync_sweep as sweep
from tests.hermes_cli import test_kanban_sync_live_resync_sweep as helpers

kanban_home = helpers.kanban_home
live = helpers.live


@pytest.mark.parametrize("kind", ["linked", "unlinked"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_link_event_boundary_spares_only_its_child(live, kind, delta):
    since = 2_000_000_000
    parent = kb.create_task(live, title="parent")
    child = kb.create_task(live, title="child with a link event at the boundary")
    sibling = kb.create_task(live, title="child with only old link events")
    kb.link_tasks(live, parent, child)
    kb.link_tasks(live, parent, sibling)
    with kbc.write_txn(live):
        live.execute("UPDATE task_events SET created_at = ?", (since - 60,))
        live.execute("UPDATE task_events SET kind = ?, created_at = ? WHERE task_id = ? AND kind = 'linked'",
                     (kind, since + delta, child))
        live.execute("DELETE FROM kanban_sync_outbox")
    held = sweep.held_at_start(live)
    assert held.links == {f"{parent}>{child}", f"{parent}>{sibling}"}
    assert not held.rows and not held.cards  # no other hold may mask the event boundary
    seen = {"tasks": {parent, child, sibling}, "task_links": set()}
    swept = sweep.sweep_absent(live, seen, helpers.BOARD, helpers.ME, since=since, held=held)
    expected = {(parent, child)} if delta >= 0 else set()
    assert {tuple(r) for r in live.execute("SELECT parent_id, child_id FROM task_links")} == expected
    assert swept == {"task_links": 1 if delta >= 0 else 2}
    tombstones = {r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_tombstones WHERE table_name = 'task_links'")}
    assert tombstones == {f"{parent}>{sibling}"} | ({f"{parent}>{child}"} if delta < 0 else set())
