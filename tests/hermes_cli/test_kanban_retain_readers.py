"""Retain custody cannot be mistaken for a failure/quota release by existing readers."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_failure_release as release, kanban_neutral_release as neutral
from hermes_cli import kanban_quota_release as quota
from tests.hermes_cli import test_kanban_failure_transport as support
from tests.hermes_cli.test_kanban_retain_transport import reserve

live = support.live


def dead_run(rid):
    dead = dispatch._DeadWorker("rate_limited", 75, "isolated quota", "rate_limited",
                                {"retry_status": "scheduled"}, rate_limited=True)
    dead.run_id = rid
    return dead


@pytest.mark.parametrize("retain_first", [True, False])
def test_real_neutral_queue_respects_both_command_orders(live, retain_first):
    tid, rid = support._running(live)
    row = live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if retain_first:
        reserve(live, tid, rid)
    with kbc.write_txn(live):
        assert neutral.queue(live, row, dead_run(rid))
    if not retain_first:
        assert reserve(live, tid, rid)["operation_kind"] == "release"
    assert neutral.pending(live) is (not retain_first)
    if retain_first:
        assert not quota.pending_task(live, tid)
        assert neutral.drain(live) == []
        if live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_neutral_effects'").fetchone():
            assert live.execute("SELECT count(*) FROM kanban_neutral_effects").fetchone()[0] == 0


def test_retained_command_never_drains_preexisting_quota_effect(live):
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    with kbc.write_txn(live):
        live.execute("CREATE TABLE kanban_neutral_effects (run_uid TEXT PRIMARY KEY, "
                     "event_kind TEXT NOT NULL, hook TEXT NOT NULL, done INTEGER DEFAULT 0)")
        hook = {"task_id": tid, "outcome": "rate_limited", "retry_status": "scheduled", "run_id": rid}
        live.execute("INSERT INTO kanban_neutral_effects (run_uid,event_kind,hook) VALUES (?,'rate_limited',?)",
                     (owned["run_uid"], json.dumps(hook)))
        live.execute("UPDATE task_runs SET ended_at=2,status='rate_limited',outcome='rate_limited' WHERE id=?", (rid,))
        kb._append_event(live, tid, "rate_limited", {}, run_id=rid)
    assert not quota.pending_task(live, tid)
    assert not neutral.pending(live)
    assert neutral.drain(live) == []
    assert live.execute("SELECT done FROM kanban_neutral_effects").fetchone()[0] == 0


def test_source_and_updater_readers_respect_retained_command(live):
    from hermes_cli import kanban_source_launch as launch, kanban_update_reclaim_intent as intent
    tid, rid = support._running(live)
    reserve(live, tid, rid)
    row = live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert intent.observer_held(live, row)
    assert launch.release_with_receipt(live, tid, rid, {}, outcome="crashed", error="isolated") == "taken"
    assert not release.queued(live, rid)
