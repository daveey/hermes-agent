"""Volatile-only flaps keep the origin's "seen not pinned here" sighting fresh enough for pickup-stall.

``kanban_pickup_stall._seen_unpinned_here_ms`` restarts a worker's stall clock at the origin's newest
``worker_queue_reason`` event whose ``lifecycle.runs_on`` does not name that worker. A card the Board keeps
re-reserving to the same worker for seconds at a time, with only volatile candidate reasons flipping, wrote such an
event on every flip before t_01a1d54acc8c8e54 throttled them to one per ``REFRESH_MS`` (600 s, the default stall
threshold). Then the sighting aged past the threshold between refreshes, and the reserved worker filed "waiting
10 min" for a card it had held for seconds (the t_01a1cfdfb6f3f7ea false-alarm class).
"""
import itertools
import json
import sqlite3

import pytest

from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_pickup_stall as base
from tests.hermes_cli import test_kanban_queue_reason_flaps as flaps
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
clock = flaps.clock
gws = base.gws
svc = base.svc
WORKER = "worker-2"


def _simulate(conn, clock, seconds):
    """The Board reserves the unpinned origin card to worker-2 every 10 s for ``seconds``, with only volatile
    reasons flipping. Returns ``(reply times, worker_queue_reason event (created_at s, payload) rows)``."""
    tid = fixtures.acknowledged(conn)
    t0 = clock[0]
    ticks = []
    for i in range(seconds // 10 + 1):
        clock[0] = t0 + 10 * i
        qr.record_reply(conn, tid, "hub", flaps.assigned(int(clock[0] * 1000), flaps.flap(i), machine=WORKER))
        ticks.append(int(clock[0]))
    rows = conn.execute("SELECT created_at, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                        (tid, qr.EVENT)).fetchall()
    return tid, ticks, [(int(r[0]), r[1]) for r in rows]


def test_the_sighting_never_ages_past_the_stall_threshold_while_flaps_continue(conn, clock):
    tid, ticks, _ = _simulate(conn, clock, 11 * 60)
    oldest = 0
    for at, following in itertools.pairwise(ticks):
        # The event log as of this reply holds until the next one; a replica sees the same rows (later).
        seen = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind=? AND created_at<=?",
                            (tid, qr.EVENT, at)).fetchone()[0]
        oldest = max(oldest, following - 1 - seen)
    print(f"11 min of 10 s same-worker volatile flaps: oldest sighting {oldest} s "
          f"(stall threshold {kps.DEFAULT_STALL_SECONDS} s)")
    # Within half the threshold plus a few replies: room for replica sync lag and a sparse reply cadence.
    assert oldest <= kps.DEFAULT_STALL_SECONDS // 2 + 30
    comments, _ = flaps.counts(conn, tid)
    assert comments <= 2  # sightings are events without comments (#2359's comment budget unchanged)


def test_twenty_minutes_of_flaps_still_post_at_most_three_comments(conn, clock):
    tid, _, _ = _simulate(conn, clock, 20 * 60)
    comments, events = flaps.counts(conn, tid)
    print(f"20 min of same-worker volatile flaps: {comments} comments, {events} events")
    assert comments <= 3 and events <= 5
    assert comments >= 2  # sightings do not hold back the REFRESH_MS comment: its clock runs from the last comment


@pytest.mark.parametrize("comment_table", ["kept", "absent (events written before the hold)"])
def test_a_chain_of_uncommented_events_keeps_the_refresh_clock_at_the_last_comment(conn, clock, comment_table):
    """Two uncommented events in a row (reservation flips) each carry the comment's time, not the previous event's,
    so a different volatile picture ``REFRESH_MS`` after the comment is still commented. Without the comment table
    (a card queued by pre-hold code) the carried ``commented_at_ms`` on the last event is the only record of it."""
    tid = fixtures.acknowledged(conn)
    commented_ms = int(clock[0] * 1000)
    qr.record_reply(conn, tid, "hub", flaps.assigned(commented_ms, flaps.flap(0)))
    assert flaps.counts(conn, tid) == (1, 1)
    for minutes, machine in ((2, "worker-3"), (4, WORKER)):  # the Board hands the reservation over and back
        clock[0] = flaps.T0 + 60 * minutes
        assert qr.record_reply(conn, tid, "hub", flaps.assigned(int(clock[0] * 1000), flaps.flap(0), machine=machine))
    assert flaps.counts(conn, tid) == (1, 3)
    chain = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, qr.EVENT)).fetchall()[1:]]
    assert [e[qr.COMMENTED_KEY] for e in chain] == [commented_ms, commented_ms]
    if comment_table != "kept":
        conn.execute(f"DROP TABLE {qr.COMMENT_TABLE}")
    clock[0] = flaps.T0 + qr.REFRESH_MS / 1000  # 6 min after the last event, 10 min after the comment
    assert qr.record_reply(conn, tid, "hub", flaps.assigned(int(clock[0] * 1000), flaps.flap(1), machine=WORKER))
    assert flaps.counts(conn, tid) == (2, 4)  # the refresh comment, not only a sighting


def test_the_reserved_worker_raises_no_alarm_at_any_tick(conn, clock, gws):
    """The origin's events, synced into worker-2's replica while the Board briefly pins the card there."""
    _, ticks, rows = _simulate(conn, clock, 11 * 60)
    hub, worker = gws
    tid = base._pinned(hub, title="re-reserved to worker-2", to=base.WORKER)
    worker.round()
    base._age(worker, tid, 2400)  # ready on the Board for 40 min, unpinned except for each brief reservation
    now = kps._now_ms() // 1000
    alarms = []
    for at, following in itertools.pairwise(ticks[59:]):  # from 10 min in: the log as of each reply, until the next
        with sqlite3.connect(wn._replica_path(worker.conn)) as rw:
            rw.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT))
            for created, payload in rows:
                if created <= at:
                    body = json.loads(payload)
                    body["machine"] = base.WORKER
                    rw.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                               (tid, qr.EVENT, json.dumps(body), now - (following - 1 - created)))
        replica = wn.open_replica(worker.conn)
        assert replica is not None
        try:
            if kps.stalled(worker.conn, replica, base.WORKER):
                alarms.append(following - 1 - ticks[0])
        finally:
            replica.close()
    print(f"stalled() on the reserved worker flagged the card at t={alarms} s")
    assert not alarms
