"""Dispatch needs a placement verdict, not a freshly ranked label backlog."""
from __future__ import annotations

import json
from unittest.mock import patch

from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_label_backlog as backlog
from hermes_cli import kanban_queue_position as position
from tests.hermes_cli import test_kanban_runs_on_dispatch as helpers

svc = helpers.svc
gws = helpers.gws


def _card(hub, index, labels=None):
    tid = kb.create_task(hub.conn, title=f"remote {index}", assignee="default",
                         runs_on=["machine:worker-1"])
    if labels:  # Legacy cards may predate the impossible-placement creation guard.
        hub.conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps(labels), tid))
        hub.conn.commit()
    return tid


def test_real_tick_does_not_rank_per_remote_card(gws):
    hub, _ = gws
    hub.enter()
    counts = []
    ids = []
    for end in (3, 7):
        ids.extend(_card(hub, i) for i in range(len(ids), end))
        with patch.object(score, "rank", wraps=score.rank) as ranked, \
                patch.object(backlog, "read", wraps=backlog.read) as read:
            result = hub.tick(dry_run=False)
        assert result.scheduling_admitted
        assert not result.spawned
        assert set(result.skipped_ineligible) == {(tid, "labels_mismatch") for tid in ids}
        assert read.call_count == 0
        counts.append(ranked.call_count)
    assert counts[0] == counts[1]


def test_real_ticks_preserve_unplaceable_event_payload_and_dedup(gws):
    hub, _ = gws
    hub.enter()
    tid = _card(hub, 0, ["missing:first"])
    expected = {"runs_on": ["missing:first"], "missing": ["missing:first"],
                "machines": ["hub", "macbook", "worker-1"]}
    for _ in range(2):
        assert hub.tick(dry_run=False).scheduling_admitted
        events = [e for e in kb.list_events(hub.conn, tid) if e.kind == "skipped_unplaceable"]
        assert [e.payload for e in events] == [expected]
    hub.conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps(["missing:second"]), tid))
    hub.conn.commit()
    assert hub.tick(dry_run=False).scheduling_admitted
    events = [e for e in kb.list_events(hub.conn, tid) if e.kind == "skipped_unplaceable"]
    assert [e.payload for e in events] == [expected, {**expected, "runs_on": ["missing:second"],
                                                    "missing": ["missing:second"]}]


def test_default_placement_keeps_backlog(gws):
    hub, _ = gws
    hub.enter()
    ids = [_card(hub, i) for i in range(3)]
    # Establish deterministic queue order independently of generated IDs/timestamps.
    for i, tid in enumerate(ids):
        hub.conn.execute("UPDATE tasks SET priority=? WHERE id=?", (100 - i, tid))
    hub.conn.commit()
    full = position.placement(hub.conn, ids[-1])
    assert full["lane"] == "machine"
    assert full["backlog"]["ahead"] == 2
    with patch.object(backlog, "read", wraps=backlog.read) as read:
        verdict = position.placement(hub.conn, ids[-1], backlog=False)
    assert read.call_count == 0
    assert verdict == {k: v for k, v in full.items() if k != "backlog"}
