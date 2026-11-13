"""Candidate load flaps are not new queue observations (t_01a1d54acc8c8e54).

On 11-13 a ready worker-first card waiting for pickup (t_01a1d5386cb55abf) got 79 ``worker-placement`` comments and
79 ``worker_queue_reason`` events in 18 min: the Board's reserve reply came back every ~10 s with the same code and
reservation, and only a candidate's ``load_threshold`` / ``max_card_runs_reached`` flipped. A flap still updates the
stored latest observation (``latest()``, the ``queue:`` line of ``kanban show``); it writes at most one event and
comment per ``REFRESH_MS``. A change of meaning (state code, eligibility, a structural reason, origin) still writes
at once.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_reason as qr
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn

T0 = 1_794_579_000.0


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(qr.time, "time", lambda: now[0])
    return now


def assigned(at, candidates, code="reserved", machine="worker-2"):
    return {"state": "assigned", "machine": machine,
            "observation": {"version": 1, "code": code, "observed_at_ms": at, "candidates": candidates}}


def queued(at, candidates, code):
    return {"state": "queued", "machine": None,
            "observation": {"version": 1, "code": code, "observed_at_ms": at, "candidates": candidates}}


# The flips seen on t_01a1d5386cb55abf at 14:17:40-14:18:42Z: worker-2 stays eligible throughout.
FLAPS = [
    {"worker-3": [], "worker-4": ["load_threshold"]},
    {"worker-3": ["max_card_runs_reached"], "worker-4": ["load_threshold", "max_card_runs_reached"]},
    {"worker-3": ["load_threshold"], "worker-4": []},
    {"worker-3": [], "worker-4": ["max_card_runs_reached"]},
]


def flap(i):
    return [{"machine": "worker-2", "reasons": []},
            *({"machine": m, "reasons": r} for m, r in FLAPS[i % len(FLAPS)].items())]


def counts(conn, tid):
    events = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT)).fetchone()[0]
    return len(kb.list_comments(conn, tid)), events


def test_volatile_flaps_every_10s_for_20_min_write_at_most_one_per_10_min(conn, clock):
    tid = fixtures.acknowledged(conn)
    last = None
    for i in range(120):  # every 10 s for 20 minutes
        clock[0] = T0 + 10 * i
        last = assigned(int(clock[0] * 1000), flap(i))
        qr.record_reply(conn, tid, "hub", last)
    comments, events = counts(conn, tid)
    print(f"20 min of 10 s volatile flaps: {comments} comments, {events} events")
    assert comments <= 3
    assert events <= 5  # plus at most one uncommented pickup-stall sighting per SIGHTING_MS (t_01a1d5a79bd2a70d)
    newest = qr.latest(conn, tid)
    assert newest["observed_at_ms"] == last["observation"]["observed_at_ms"]
    assert newest["candidates"] == qr.normalize(last)["candidates"]
    assert newest["current"] is True
    assert qr.describe(newest).endswith(qr._stamp(last["observation"]["observed_at_ms"]))


def test_refresh_needs_a_different_volatile_picture(conn, clock):
    """After the refresh window a flap that ends where the last comment was writes nothing new."""
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] = T0 + 30
    qr.record_reply(conn, tid, "hub", assigned(2, flap(1)))
    clock[0] = T0 + qr.REFRESH_MS / 1000 + 60
    assert not qr.record_reply(conn, tid, "hub", assigned(3, flap(0)))  # same picture as the one commented
    assert counts(conn, tid) == (1, 1)
    assert qr.latest(conn, tid)["observed_at_ms"] == 3
    clock[0] += 10
    assert qr.record_reply(conn, tid, "hub", assigned(4, flap(2)))
    assert counts(conn, tid) == (2, 2)


def test_state_code_changes_still_comment_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] += 5
    qr.record_reply(conn, tid, "hub", assigned(2, flap(1)))  # volatile only: stored, no comment
    clock[0] += 5
    assert qr.record_error(conn, tid, "hub", status=400)
    clock[0] += 5
    assert qr.record_reply(conn, tid, "hub", queued(4, [{"machine": "worker-2", "reasons": ["last_seen_stale"]}],
                                                    "no_live_candidate"))
    bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert len(bodies) == 3
    assert bodies[1].startswith("Board observation failed") and bodies[2].startswith("No live candidate")
    assert qr.latest(conn, tid)["code"] == "no_live_candidate"


def test_all_candidates_becoming_ineligible_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] += 10
    none_eligible = [{"machine": m, "reasons": ["load_threshold"]} for m in ("worker-2", "worker-3", "worker-4")]
    assert qr.record_reply(conn, tid, "hub", assigned(2, none_eligible))
    assert counts(conn, tid) == (2, 2)
    clock[0] += 10
    assert qr.record_reply(conn, tid, "hub", assigned(3, flap(0)))  # and back
    assert counts(conn, tid) == (3, 3)


def test_gaining_or_losing_a_structural_reason_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] += 10
    gained = flap(0)
    gained[1] = {"machine": "worker-3", "reasons": ["missing_labels"], "missing_labels": ["skill:five-whys"]}
    assert qr.record_reply(conn, tid, "hub", assigned(2, gained))
    assert "missing skill:five-whys" in kb.list_comments(conn, tid)[-1].body
    clock[0] += 10
    draining = flap(0)
    draining[2] = {"machine": "worker-4", "reasons": ["draining", "load_threshold"]}
    gained[2] = draining[2]
    assert qr.record_reply(conn, tid, "hub", assigned(3, gained))
    clock[0] += 10
    assert qr.record_reply(conn, tid, "hub", assigned(4, flap(0)))  # both lost
    assert counts(conn, tid) == (4, 4)


def test_return_to_origin_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] += 10
    origin = {"state": "origin", "machine": "hub", "reason": "no pool worker can run it: missing git",
              "observation": {"version": 1, "code": "no_eligible_worker", "observed_at_ms": 2,
                              "candidates": flap(1)}}
    assert qr.record_reply(conn, tid, "hub", origin)
    assert counts(conn, tid) == (2, 2)
    assert kb.list_comments(conn, tid)[-1].body.startswith("Returned to origin hub")
    assert qr.latest(conn, tid)["state"] == "origin"


def test_a_new_reserved_worker_is_an_event_without_a_comment(conn, clock):
    """pickup-stall reads the origin's events as "seen not pinned here" sightings, so a reservation handed to another
    worker stays in the event log; its text would repeat the last comment, so it posts none."""
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    clock[0] += 10
    assert qr.record_reply(conn, tid, "hub", assigned(2, flap(0), machine="worker-3"))
    assert counts(conn, tid) == (1, 2)
    assert qr.latest(conn, tid)["machine"] == "worker-3"


def test_latest_without_the_local_table_reads_events(conn):
    """A replica (or an origin before its first flap) has no side table: latest() is the last event."""
    tid = fixtures.acknowledged(conn)
    conn.execute(f"DROP TABLE IF EXISTS {qr.LATEST_TABLE}")
    qr.record_reply(conn, tid, "hub", assigned(1, flap(0)))
    conn.execute(f"DROP TABLE IF EXISTS {qr.LATEST_TABLE}")
    assert qr.latest(conn, tid)["observed_at_ms"] == 1
