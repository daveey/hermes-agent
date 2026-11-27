"""Minute occupancy observations; advisory only, never consulted by admission. [daveey]"""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import sqlite3
import time

from hermes_cli import kanban_lanes as lanes

_current = ContextVar("lane_sample", default=None)
_log = logging.getLogger(__name__)
RETENTION = 8 * 86400


class Capture(dict):
    """lane -> task IDs refused by a cap this pass (a set dedups repeat attempts), plus the
    occupancy each refusal observed in ``witness`` (lane -> highest running count)."""

    def __init__(self, lane_names):
        super().__init__((name, set()) for name in lane_names)
        self.witness = {}


def rejected(ids, names, default, running=None):
    """Only real cap-filter decisions inside an admitted dispatcher pass count.

    Advisory: a malformed argument can never raise into the admission path that called this.
    """
    capture = _current.get()
    if capture is None:
        return
    try:
        for task_id in ids:
            lane = names.get(task_id, default)
            if lane in capture:
                capture[lane].add(task_id)
                if running is not None:
                    seen = getattr(capture, "witness", None)
                    if seen is not None:
                        seen[lane] = max(seen.get(lane, 0), running)
    except (AttributeError, LookupError, TypeError, ValueError):
        _log.warning("lane rejection capture failed", exc_info=True)


def rejected_at_claim(lane, task_id, running):
    """The claim-time cap recheck refused ``task_id`` while ``lane`` held ``running`` others."""
    rejected([task_id], {task_id: lane}, lane, running)


@contextmanager
def observe(conn, *, dry_run=False):
    pol = lanes.safe_policy()
    if dry_run or pol is None or pol.shadow:
        yield
        return
    try:
        counts = lanes._running(conn, pol)
    except (sqlite3.Error, OSError):
        _log.warning("lane occupancy census unavailable", exc_info=True)
        yield
        return
    capture = Capture(pol.lanes)
    token = _current.set(capture)
    try:
        yield
    finally:
        _current.reset(token)
        # Instrumentation failure cannot change dispatch's admission result.
        try:
            record(conn, pol, counts, capture)
        except (sqlite3.Error, OSError):
            _log.warning("lane occupancy sample unavailable", exc_info=True)


UPSERT = (
    "INSERT INTO lane_samples VALUES (?,?,?,?,?,?) ON CONFLICT(ts,lane) DO UPDATE SET "
    # Preserve the occupancy/cap pair that witnessed rejection, even if the
    # lane drains or its cap changes later in this minute. Otherwise keep peak occupancy.
    "running=CASE WHEN excluded.rejected>0 OR (lane_samples.rejected=0 AND "
    "excluded.running>=lane_samples.running) THEN excluded.running ELSE lane_samples.running END, "
    "cap=CASE WHEN excluded.rejected>0 OR (lane_samples.rejected=0 AND "
    "excluded.running>=lane_samples.running) THEN excluded.cap ELSE lane_samples.cap END, "
    "ready=MAX(lane_samples.ready,excluded.ready), rejected=lane_samples.rejected+excluded.rejected")
SCHEMA = ("CREATE TABLE IF NOT EXISTS lane_samples (ts INTEGER NOT NULL, lane TEXT NOT NULL, "
          "running INTEGER NOT NULL, cap INTEGER, ready INTEGER NOT NULL, rejected INTEGER NOT NULL, "
          "PRIMARY KEY(ts,lane))")


def record(conn, pol, counts, capture, *, now=None):
    minute = int(time.time() if now is None else now) // 60 * 60
    ready = Counter(pol.lane_of(r[0]) for r in conn.execute(
        "SELECT tenant FROM tasks WHERE status='ready' AND claim_lock IS NULL"))
    witness = getattr(capture, "witness", {})
    # A refusal observed occupancy at or above the cap; the pass-start census may predate this
    # pass's own claims.
    rows = [(minute, name, max(counts[name], witness.get(name, 0)) if capture[name] else counts[name],
             spec.max_workers, ready[name], len(capture[name])) for name, spec in pol.lanes.items()]
    # One advisory write unit. A SAVEPOINT nests inside a caller's transaction without
    # committing or rolling back anything the caller owns, and commits itself when outermost.
    conn.execute("SAVEPOINT lane_samples_record")
    try:
        conn.execute(SCHEMA)
        conn.executemany(UPSERT, rows)
        # PRIMARY KEY(ts,lane) is the ts-prefixed index this range delete uses.
        conn.execute("DELETE FROM lane_samples WHERE ts < ?", (minute - RETENTION,))
    except BaseException:
        conn.execute("ROLLBACK TO lane_samples_record")
        conn.execute("RELEASE lane_samples_record")
        raise
    conn.execute("RELEASE lane_samples_record")


def ticks(conn, pol, *, window_s=86400, count=48, now=None):
    """Return None on pre-samples cores; missing observed buckets retain unknown data.

    Within a bucket choose the peak observed occupancy state, retaining its cap.
    Historical run overlap supplies occupancy only, never a historical cap or zero rejections.
    """
    if not 1 <= count <= 336 or not 0 < window_s <= RETENTION:
        raise ValueError("invalid occupancy window")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lane_samples'").fetchone():
        return None
    now = int(time.time() if now is None else now)
    width = window_s / count
    start = now - window_s
    out = {name: [dict(t=int(start + i * width), state='unknown', running=None, cap=None,
                       rejected=None) for i in range(count)] for name in pol.lanes}
    observed = {}
    for row in conn.execute("SELECT ts,lane,running,cap,rejected FROM lane_samples WHERE ts>=? AND ts<=? ORDER BY ts",
                            (int(start) // 60 * 60, now)):
        ts, name, running, cap, rejects = row
        if name not in out:
            continue
        index = max(0, min(count - 1, int((ts - start) / width)))
        state = ('rejecting' if cap is not None and running >= cap and rejects else
                 'full' if running > 0 and cap is not None and running >= cap else
                 'partial' if running else 'empty')
        rank = {'empty': 0, 'partial': 1, 'full': 2, 'rejecting': 3}
        tick = out[name][index]
        tick['rejected'] = (tick['rejected'] or 0) + rejects
        # Same severity: the busier minute is the witness; equal busyness takes the latest cap.
        if tick['state'] == 'unknown' or (rank[state], running) >= (rank[tick['state']], tick['running']):
            tick.update(state=state, running=running, cap=cap)
        observed[(name, index)] = True
    # Sweep real run endpoints per historical bucket: counting overlapping runs
    # would overstate concurrency when several sequential jobs share a bucket.
    endpoints = {name: [] for name in pol.lanes}
    for tenant, began, ended in conn.execute(
            "SELECT t.tenant,r.started_at,COALESCE(r.ended_at,?) FROM task_runs r "
            "JOIN tasks t ON t.id=r.task_id WHERE r.started_at<? AND COALESCE(r.ended_at,?)>?",
            (now, now, now, start)):
        left, right = max(start, began), min(now, ended)
        if left < right:  # a zero-length or inverted span proves no occupancy
            endpoints[pol.lane_of(tenant)].append((left, right))
    for name, spans in endpoints.items():
        for index, tick in enumerate(out[name]):
            if (name, index) in observed:
                continue
            left, right = start + index * width, start + (index + 1) * width
            events = [(max(left, a), 1) for a, b in spans if a < right and b > left]
            events += [(min(right, b), -1) for a, b in spans if a < right and b > left]
            running = peak = 0
            for _, delta in sorted(events):
                running += delta
                peak = max(peak, running)
            # No run is not proof the dispatcher observed an empty lane.
            if events:
                tick.update(state='partial', running=peak, rejection_unknown=True)
    return out
