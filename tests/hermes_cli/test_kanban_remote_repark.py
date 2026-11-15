"""A remote re-park imported at the card's home never replays the previous park's wake. [daveey]

Board t_01a1df6a505bf42f (5 of 516 parks in 24 h, 2026-11-14/15): worker-5 parked hub card t_01a1ddc5120760b0 for
CI at 12:59:21 (``continued``, wake_at 13:14:21 plus a wake_check). The hub's feed pull applied the task row
(``status=scheduled``) in one page and the ``continued`` event in the next; between them the hub's park sweep read
the latest parking event it had -- the previous clock park, whose 11:34:00 wake had already fired at 11:34:20 -- and
woke the card again (``continuation_woken wake_at=1794742440``) before the new park arrived. The card sat ``ready``
through its CI wait.

The fence: a parking event followed by a later run's ``claimed``/``spawned`` or by a ``continuation_woken`` is spent;
it never wakes a ``scheduled`` card again, which waits for its own park (or, if that never arrives, a late backstop).
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support

WORKER = "worker-5"
R0, R1 = "01a1dd00-0000-7000-8000-000000000000", "01a1df48-8ea7-7a85-955d-ff5f05083d41"
PROBE = "hermes --run-module hermes_cli.kanban_pr_wake https://github.com/o/r/pull/3281"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def home(kanban_home):
    """The card's home board: a synced board whose own gateway is ``hub``."""
    for conn in support.synced_conn(kanban_home):
        conn.execute("UPDATE kanban_sync_state SET gateway_id = 'hub'")
        conn.commit()
        yield conn


class Feed:
    def __init__(self, conn):
        self.conn, self.seq, self.version = conn, 0, 0

    def _ch(self, table, uid, fields, version=1):
        self.seq += 1
        return sync.RemoteChange(self.seq, table, uid, "upsert", fields, version,
                                 f"{self.seq:013d}-0000-{WORKER}", WORKER, None)

    def row(self, tid, **fields):
        self.version += 1
        return self._ch("tasks", tid, fields, version=self.version)

    def event(self, tid, kind, created_at, payload, run_uid=None):
        fields = {"task_id": tid, "kind": kind, "payload": json.dumps(payload), "created_at": created_at}
        if run_uid:
            fields["run_uid"] = run_uid
        return self._ch("task_events", f"01a1df6a-{self.seq + 1:04d}-7000-8000-000000000000", fields)

    def apply(self, *changes):
        sync.apply_remote_changes(self.conn, list(changes), new_cursor=self.seq)


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def _woken(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == kc.CONTINUATION_WOKEN_EVENT]


def _card_whose_clock_park_fired(conn, now, *, fired=True):
    """A home card: parked by run R0 with a clock wake already past, woken by this home sweep (``fired``),
    then claimed by run R1 on a worker. Returns (feed, task id)."""
    feed, tid = Feed(conn), "t_01a1ddc5120760b0"
    old_wake = now - 5000
    feed.apply(
        feed.row(tid, title="land PR", status="scheduled", created_at=now - 9000, workspace_kind="scratch",
                 assignee="default"),
        feed.event(tid, "scheduled", now - 8000, {"reason": "SCHEDULED_UNTIL=...", "wake_at": old_wake}, R0),
    )
    if fired:
        assert kc.wake_due_continuations(conn, now=now) == [tid]
        assert _status(conn, tid) == "ready"
    else:
        feed.apply(feed.row(tid, status="ready"))  # e.g. a human unblock elsewhere ended the park
    feed.apply(
        feed.event(tid, "claimed", now - 120, {"holder": WORKER, "run_uid": R1}, R1),
        feed.event(tid, "spawned", now - 120, {"pid": 1}, R1),
        feed.row(tid, status="running", current_run_uid=R1),
    )
    assert _status(conn, tid) == "running"
    return feed, tid


def _new_park(feed, tid, now, wake_at):
    row = feed.row(tid, status="scheduled", current_run_uid=None)
    event = feed.event(tid, "continued", now, {
        "continuation": 1, "status": "scheduled", "resume_status": "ready", "wake_at": wake_at,
        "wake_check": PROBE, "check_every": 120, "probe_host": WORKER,
        "awaiting": {"kind": "ci", "ref": "https://github.com/o/r/actions/runs/1"}}, R1)
    return row, event


@pytest.mark.parametrize("fired", [True, False], ids=["previous-wake-fired", "previous-park-ended-otherwise"])
@pytest.mark.parametrize("order", ["row-first", "event-first"])
def test_an_imported_repark_never_replays_the_previous_parks_wake(home, order, fired):
    now = int(time.time())
    feed, tid = _card_whose_clock_park_fired(home, now, fired=fired)
    before = len(_woken(home, tid))
    new_wake = now + 900
    row, event = _new_park(feed, tid, now, new_wake)
    first, second = (row, event) if order == "row-first" else (event, row)
    feed.apply(first)                                       # the page boundary the hub hit at 12:59:21
    assert kc.wake_due_continuations(home, now=now) == []
    feed.apply(second)
    for t in (now, now + 1, new_wake - 1):
        assert kc.wake_due_continuations(home, now=t) == []
        assert _status(home, tid) == "scheduled"
    assert len(_woken(home, tid)) == before
    park = kc.park_state(home, tid)
    assert park["timed"] and park["payload"]["wake_at"] == new_wake
    assert kc.wake_due_continuations(home, now=new_wake) == [tid]    # its own wake still lands
    assert _status(home, tid) == "ready"
    assert _woken(home, tid)[-1]["wake_at"] == new_wake


def test_a_scheduled_row_whose_park_never_arrives_wakes_only_at_the_late_backstop(home):
    """Bounded: a lost park event cannot strand the card in ``scheduled``, and it never blocks it for a human."""
    now = int(time.time())
    feed, tid = _card_whose_clock_park_fired(home, now)
    home.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid,))  # a stale kind from an old block
    home.commit()
    row, _ = _new_park(feed, tid, now, now + 900)
    feed.apply(row)
    park = kc.park_state(home, tid)
    assert park["superseded"] and not park["timed"]
    backstop = park["parked_at"] + kc.SUPERSEDED_PARK_GRACE_SECONDS
    assert kc.wake_due_continuations(home, now=backstop - 1) == []
    assert _status(home, tid) == "scheduled"
    assert kc.wake_due_continuations(home, now=backstop) == [tid]
    assert _status(home, tid) == "ready"
    woken = _woken(home, tid)[-1]
    assert woken["park_missing"] is True and woken["wake_at"] == backstop


def test_a_park_from_the_run_that_claimed_after_it_arrived_is_not_spent(home):
    """The fence keys on a LATER run: the parking run's own claim event, imported after its park, does not void it."""
    now = int(time.time())
    feed, tid = Feed(home), "t_01a1df6a00000001"
    feed.apply(feed.row(tid, title="x", status="running", current_run_uid=R1, created_at=now - 100,
                        workspace_kind="scratch", assignee="default"))
    row, event = _new_park(feed, tid, now, now + 60)
    feed.apply(row, event)
    feed.apply(feed.event(tid, "claimed", now - 50, {"holder": WORKER, "run_uid": R1}, R1))
    park = kc.park_state(home, tid)
    assert park["timed"] and not park["superseded"]
    assert kc.wake_due_continuations(home, now=now + 60) == [tid]


def test_the_wake_rechecks_the_park_inside_its_write_transaction(home, monkeypatch):
    """A park read before the lock but spent by the time the sweep writes is not woken."""
    now = int(time.time())
    feed, tid = Feed(home), "t_01a1df6a00000002"
    feed.apply(
        feed.row(tid, title="x", status="scheduled", created_at=now - 100, workspace_kind="scratch",
                 assignee="default"),
        feed.event(tid, "scheduled", now - 90, {"reason": "r", "wake_at": now - 10}, R0),
    )
    real = kc.park_state
    calls = []

    def stale_read(conn, task_id):
        calls.append(task_id)
        out = real(conn, task_id)
        if len(calls) == 1:  # the sweep's read; then a later run's claim lands before its write
            feed.apply(feed.event(tid, "claimed", now - 5, {"holder": WORKER, "run_uid": R1}, R1))
        return out

    monkeypatch.setattr(kc, "park_state", stale_read)
    assert kc.wake_due_continuations(home, now=now) == []
    assert _status(home, tid) == "scheduled"
    assert _woken(home, tid) == []
