"""A new wait reason gets its thread comment only once it has held for ``HOLD_MS`` (t_01a1d61f47e3f1a0).

#2359 stopped volatile-only flaps from commenting, but the real 11-13 flood replayed through it still wrote 42
``worker-placement`` comments on t_01a1d5386cb55abf (14 in one 10-minute window) and 16 on t_01a1d54acc8c8e54: meaning
changes that revert within about a minute (all candidates briefly ineligible, a worker draining for its own core
update, a 41 s board_error blip). Every meaning change is still an event at once (pickup-stall sightings and
``latest()`` stay exact); only the comment waits until the new meaning has held.

The fixture holds the 197 stored ``worker_queue_reason`` payloads of both cards from the MacBook board, in recorded
order. Each is fed at its recorded time and re-sent every 10 s until the next one, as the Board's reserve replies did.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_reason as qr
from tests.hermes_cli import test_kanban_queue_reason_flaps as flaps
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
clock = flaps.clock

FIXTURE = Path(__file__).parent / "fixtures" / "queue-reason-flood-2026-11-13.json"
STREAMS = json.loads(FIXTURE.read_text())["streams"]
REPEAT_S = 10
WINDOW_S = 600


def _comments(conn, tid):
    return [c for c in kb.list_comments(conn, tid) if c.author == "worker-placement"]


@pytest.fixture
def no_comment_hold(monkeypatch):
    """For tests that pin WHICH observations are commented, not when: every meaning change is commented at once,
    as before the hold. This module pins the hold itself."""
    monkeypatch.setattr(qr, "HOLD_MS", 0)


def _send(conn, tid, payload):
    value = {k: v for k, v in payload.items() if k not in ("recorded_at_ms", "lifecycle")}
    fence = {"row": qr.snapshot(conn, tid)["row"], "lifecycle": payload["lifecycle"]}
    return qr._record(conn, tid, "hub", value, fence=fence)


def replay(conn, clock, stream, tail_s=300):
    """Feed ``stream`` with 10 s repeats; returns ``(task id, comment times in s)``."""
    tid = fixtures.acknowledged(conn)
    times = []
    ends = [p["recorded_at_ms"] for p in stream[1:]] + [stream[-1]["recorded_at_ms"] + tail_s * 1000]
    for payload, end_ms in zip(stream, ends):
        at_ms = payload["recorded_at_ms"]
        while at_ms < end_ms:
            clock[0] = at_ms / 1000
            before = len(_comments(conn, tid))
            _send(conn, tid, payload)
            times.extend([clock[0]] * (len(_comments(conn, tid)) - before))
            at_ms += REPEAT_S * 1000
    return tid, times


def _worst_window(times):
    return max((sum(1 for u in times if t <= u < t + WINDOW_S) for t in times), default=0)


@pytest.mark.parametrize("task", sorted(STREAMS))
def test_the_11_13_flood_replay_comments_only_held_reasons(conn, clock, task):
    stream = STREAMS[task]
    tid, times = replay(conn, clock, stream)
    worst = _worst_window(times)
    print(f"{task}: {len(stream)} observations -> {len(times)} comments, worst 10 min {worst}")
    assert len(times) <= 8
    assert worst <= 3
    # Events are unchanged: every meaning change is still appended, and latest() is the newest observation.
    events = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT)).fetchone()[0]
    assert events >= len(times)
    newest = qr.latest(conn, tid)
    last = stream[-1]
    assert newest.get("code") == last["code"] and newest.get("candidates") == last["candidates"]


def test_the_first_observation_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    assert qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    assert len(_comments(conn, tid)) == 1


def _structural(at):
    gained = flaps.flap(0)
    gained[1] = {"machine": "worker-3", "reasons": ["missing_labels"], "missing_labels": ["skill:five-whys"]}
    return flaps.assigned(at, gained)


def test_a_persisting_structural_change_comments_within_the_hold_plus_one_reply(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    clock[0] += 10
    changed_at = clock[0]
    assert qr.record_reply(conn, tid, "hub", _structural(2))  # the event is appended at once
    assert len(_comments(conn, tid)) == 1  # its comment waits
    commented_at = confirming = None
    for i in range(1, 30):
        clock[0] = changed_at + REPEAT_S * i
        qr.record_reply(conn, tid, "hub", _structural(2 + i))  # the Board's repeats confirm the hold
        if len(_comments(conn, tid)) == 2:
            commented_at, confirming = clock[0], 2 + i
            break
    assert commented_at is not None and commented_at - changed_at <= qr.HOLD_MS / 1000 + REPEAT_S
    assert commented_at - changed_at >= qr.HOLD_MS / 1000
    body = _comments(conn, tid)[-1].body
    assert "missing skill:five-whys" in body
    assert body.endswith(qr._stamp(confirming))  # describe() of the confirming observation


def test_a_change_that_reverts_before_the_hold_writes_events_but_no_comment(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    for i in range(1, 6):
        clock[0] += 10
        qr.record_reply(conn, tid, "hub", _structural(1 + i))
    clock[0] += 10
    assert qr.record_reply(conn, tid, "hub", flaps.assigned(10, flaps.flap(0)))  # back: an event
    for i in range(30):
        clock[0] += 10
        qr.record_reply(conn, tid, "hub", flaps.assigned(11 + i, flaps.flap(0)))
    assert len(_comments(conn, tid)) == 1
    events = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT)).fetchone()[0]
    assert events == 3


def test_a_second_change_restarts_the_hold(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    clock[0] += 10
    qr.record_reply(conn, tid, "hub", _structural(2))
    clock[0] += 100
    none_eligible = [{"machine": m, "reasons": ["load_threshold"]} for m in ("worker-2", "worker-3", "worker-4")]
    second_at = clock[0]
    assert qr.record_reply(conn, tid, "hub", flaps.assigned(3, none_eligible))
    clock[0] = second_at + qr.HOLD_MS / 1000 - 10
    qr.record_reply(conn, tid, "hub", flaps.assigned(4, none_eligible))
    assert len(_comments(conn, tid)) == 1  # the first change never held; the second has not yet
    clock[0] = second_at + qr.HOLD_MS / 1000
    qr.record_reply(conn, tid, "hub", flaps.assigned(5, none_eligible))
    assert len(_comments(conn, tid)) == 2


def test_a_new_run_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    first = {"version": 1, "code": "reserved", "state": "assigned", "machine": "worker-2",
             "candidates": [{"machine": "worker-2", "reasons": []}]}
    qr._record(conn, tid, "hub", first, fence={"row": qr.snapshot(conn, tid)["row"],
                                               "lifecycle": {"runs_on": None, "run_count": 0}})
    clock[0] += 10
    assert qr._record(conn, tid, "hub", first, fence={"row": qr.snapshot(conn, tid)["row"],
                                                      "lifecycle": {"runs_on": None, "run_count": 1}})
    assert len(_comments(conn, tid)) == 2


def test_return_to_origin_comments_at_once(conn, clock):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    clock[0] += 10
    origin = {"state": "origin", "machine": "hub", "reason": "no pool worker can run it: missing git",
              "observation": {"version": 1, "code": "no_eligible_worker", "observed_at_ms": 2,
                              "candidates": flaps.flap(1)}}
    assert qr.record_reply(conn, tid, "hub", origin)
    assert len(_comments(conn, tid)) == 2


def test_a_sustained_board_outage_is_commented_once_held_and_its_recovery_too(conn, clock):
    """Each failing pass re-records the confirmed board_error: those repeats confirm the hold."""
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", flaps.assigned(1, flaps.flap(0)))
    for _ in range(qr.HOLD_MS // 10_000 + 2):
        clock[0] += 10
        qr.record_error(conn, tid, "hub", error=TimeoutError(), stage="reserve")
    assert qr.latest(conn, tid)["code"] == "board_error"
    assert [c.body.split(" — ")[0] for c in _comments(conn, tid)][-1] == "Board observation failed"
    for i in range(qr.HOLD_MS // 10_000 + 1):
        clock[0] += 10
        qr.record_reply(conn, tid, "hub", flaps.assigned(10 + i, flaps.flap(0)))
    assert len(_comments(conn, tid)) == 3
    assert _comments(conn, tid)[-1].body.startswith("Worker reserved")
