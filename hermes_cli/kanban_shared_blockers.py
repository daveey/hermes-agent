"""Shared external blockers: park many cards behind ONE blocker and probe it once. [daveey]

A worker that hits a wall outside its card (CI billing refused, a provider outage, a release
pipeline down) ends its run with ``kanban_block(..., blocker_id="<slug>")`` or
``kanban_continue(..., blocker_id="<slug>")``. The id lands in that run's metadata
(``blocker_id``); nothing else is stored, so the blocker's state is derived from run history:

* a card is **behind** blocker X while its latest non-neutral closed run carries X;
* X was **asserted** at the newest ``ended_at`` of any run carrying X;
* X was **cleared** at the newest ``ended_at`` of a run that followed an X-run on the same card
  and did NOT carry X (it completed, handed off, or stopped on something else);
* X is **open** while asserted > cleared.

While X is open the dispatcher spawns at most one card behind it (the *probe*) per
``kanban.shared_blocker_probe_seconds``; the others are held with respawn guard reason
``shared_blocker`` instead of each re-discovering the same wall. A human unblock of a card lets
that card probe at once (still one probe at a time). When a probe ends without re-asserting X,
X clears: held ``ready`` cards dispatch normally and cards still ``blocked`` behind X are
unblocked by :func:`release_cleared_blockers`.

Neutral outcomes (crash, timeout, reclaim, spawn failure, rate limit, give-up) say nothing about
the external condition, so they neither assert nor clear a blocker.

Matching on normalized error text was considered and left out: crash/timeout/protocol-violation
errors share fixed preambles across unrelated cards (``_error_fingerprint`` keeps 80 chars), and
LLM-written block reasons never match verbatim. Same-tick identical crash waves are already
tripped by ``_account_crashes``.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

GUARD_REASON = "shared_blocker"
CLEARED_EVENT = "shared_blocker_cleared"
DEFAULT_PROBE_SECONDS = 1800
MAX_BLOCKER_ID_CHARS = 64
# Outcomes that prove nothing about an external condition.
NEUTRAL_OUTCOMES = frozenset({
    "crashed", "timed_out", "reclaimed", "spawn_failed", "rate_limited", "gave_up", "released",
})
# Only blockers asserted this recently are considered (bounds the scan on old boards).
_LOOKBACK_SECONDS = 14 * 86400

_ID_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_blocker_id(raw) -> Optional[str]:
    """Lower-case slug (``[a-z0-9._-]``, max 64 chars) or None for empty/invalid input."""
    if not isinstance(raw, str):
        return None
    slug = _ID_RE.sub("-", raw.strip().lower()).strip("-._")
    return slug[:MAX_BLOCKER_ID_CHARS] or None


def probe_interval_seconds(cfg: Optional[dict] = None) -> int:
    """``kanban.shared_blocker_probe_seconds`` (>= 0); 0 = no spacing, still one probe at a time."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("shared_blocker_probe_seconds", DEFAULT_PROBE_SECONDS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_PROBE_SECONDS


@dataclass
class BlockerState:
    blocker_id: str
    asserted_at: int = 0
    cleared_at: int = 0
    cleared_by: Optional[tuple[str, int]] = None   # (task_id, run_id) of the clearing run
    reason: str = ""                                # summary of the newest asserting run
    # task_id -> ended_at of the asserting run the card is still behind
    behind: dict[str, int] = field(default_factory=dict)
    # task_id -> started_at of the newest run started after the card's asserting run
    probe_starts: dict[str, int] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.asserted_at > self.cleared_at


def _meta_blocker(kb, metadata) -> Optional[str]:
    return normalize_blocker_id(kb._json_dict(metadata).get("blocker_id"))


def blocker_states(conn: sqlite3.Connection, *, now: Optional[int] = None) -> dict[str, BlockerState]:
    """Derive every recently asserted blocker's state from ``task_runs`` (read-only)."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    asserting = conn.execute(
        "SELECT id, task_id, ended_at, metadata, summary FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at >= ? AND metadata LIKE '%\"blocker_id\"%' "
        "ORDER BY id",
        (now - _LOOKBACK_SECONDS,),
    ).fetchall()
    states: dict[str, BlockerState] = {}
    task_ids: set[str] = set()
    for row in asserting:
        bid = _meta_blocker(kb, row["metadata"])
        if bid is None:
            continue
        st = states.setdefault(bid, BlockerState(bid))
        ended = kb._to_epoch(row["ended_at"]) or 0
        if ended >= st.asserted_at:
            st.asserted_at = ended
            st.reason = kb._first_line(row["summary"], 300)
        task_ids.add(row["task_id"])
    if not states:
        return states
    for tid in task_ids:
        status_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        live = status_row is not None and status_row["status"] not in ("done", "archived")
        runs = conn.execute(
            "SELECT id, outcome, started_at, ended_at, metadata FROM task_runs "
            "WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        current: Optional[tuple[str, int]] = None   # (blocker, asserting ended_at)
        for run in runs:
            started = kb._to_epoch(run["started_at"]) or 0
            if current is not None and current[0] in states:
                st = states[current[0]]
                st.probe_starts[tid] = max(st.probe_starts.get(tid, 0), started)
            if run["ended_at"] is None:
                continue
            ended = kb._to_epoch(run["ended_at"]) or 0
            bid = _meta_blocker(kb, run["metadata"])
            if bid is not None:
                if current is not None and current[0] != bid and current[0] in states:
                    _mark_cleared(states[current[0]], tid, run["id"], ended)
                current = (bid, ended)
                continue
            if (run["outcome"] or "") in NEUTRAL_OUTCOMES:
                continue
            if current is not None and current[0] in states:
                _mark_cleared(states[current[0]], tid, run["id"], ended)
            current = None
        if current is not None and live and current[0] in states:
            states[current[0]].behind[tid] = current[1]
    for st in states.values():
        # Probe starts only count once the card is (still) behind the blocker.
        st.probe_starts = {t: s for t, s in st.probe_starts.items() if t in st.behind}
    return states


def _mark_cleared(st: BlockerState, task_id: str, run_id: int, ended: int) -> None:
    if ended >= st.cleared_at:
        st.cleared_at = ended
        st.cleared_by = (task_id, int(run_id))


def blocker_for_task(states: dict[str, BlockerState], task_id: str) -> Optional[BlockerState]:
    for st in states.values():
        if task_id in st.behind:
            return st
    return None


def _unblocked_since_assertion(conn: sqlite3.Connection, task_id: str, blocker_id: str) -> bool:
    """True when ``task_id`` was unblocked after the newest event asserting ``blocker_id``
    (event ids, not seconds: a bulk unblock and a re-assertion often share a second)."""
    from hermes_cli import kanban_db as kb

    newest = 0
    for row in conn.execute(
        "SELECT id, payload FROM task_events WHERE kind IN ('blocked', 'continued', 'block_loop_detected') "
        "AND payload LIKE ? ORDER BY id DESC LIMIT 50", (f'%"{blocker_id}"%',),
    ):
        if normalize_blocker_id(kb._json_dict(row["payload"]).get("blocker_id")) == blocker_id:
            newest = int(row["id"])
            break
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'unblocked' AND id > ? LIMIT 1",
        (task_id, newest),
    ).fetchone() is not None


def shared_blocker_guard(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
    interval: Optional[int] = None,
) -> Optional[str]:
    """:data:`GUARD_REASON` when ``task_id`` sits behind an open blocker and must wait for the
    probe; None when it may spawn (no blocker, blocker cleared, or this card is the probe)."""
    now = int(time.time()) if now is None else int(now)
    states = blocker_states(conn, now=now)
    st = blocker_for_task(states, task_id)
    if st is None or not st.is_open:
        return None
    others = [t for t in st.behind if t != task_id]
    if others:
        marks = ",".join("?" * len(others))
        if conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({marks}) AND status = 'running' LIMIT 1", others,
        ).fetchone():
            return GUARD_REASON   # a probe is already in flight
    if interval is None:
        interval = probe_interval_seconds()
    last = max([st.asserted_at, *st.probe_starts.values()])
    if now - last >= interval:
        return None
    # A human unblock after the newest assertion is a deliberate "try again now".
    if _unblocked_since_assertion(conn, task_id, st.blocker_id):
        return None
    return GUARD_REASON


def release_cleared_blockers(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[str]:
    """Unblock ``blocked`` cards behind a blocker a later probe cleared. Returns task ids."""
    from hermes_cli import kanban_db as kb

    released: list[str] = []
    for st in blocker_states(conn, now=now).values():
        if st.is_open or st.cleared_by is None:
            continue
        for tid, asserted in sorted(st.behind.items()):
            if asserted >= st.cleared_at:
                continue
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None or row["status"] != "blocked":
                continue
            if not kb.unblock_task(conn, tid):
                continue
            by_task, by_run = st.cleared_by
            with kb.write_txn(conn):
                kb._append_event(conn, tid, CLEARED_EVENT, {
                    "blocker_id": st.blocker_id, "cleared_by_task": by_task, "cleared_by_run": by_run,
                })
                kb._insert_comment(
                    conn, tid, "dispatcher",
                    f"shared blocker `{st.blocker_id}` cleared: {by_task} run {by_run} ended without "
                    "hitting it, so this card was unblocked automatically.",
                    int(time.time()),
                )
            released.append(tid)
    return released


def render_blocker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Worker-context section naming open blockers so workers reuse the same id."""
    states = [s for s in blocker_states(conn).values() if s.is_open]
    if not states:
        return ""
    lines = ["## Open shared blockers on this board",
             "External walls other cards already hit. If yours is one of them, stop early and end "
             "with kanban_block/kanban_continue passing the same `blocker_id` instead of rediscovering "
             "it; only one card probes a blocker at a time."]
    mine = None
    for st in sorted(states, key=lambda s: -s.asserted_at):
        if task_id in st.behind:
            mine = st
        lines.append(f"- `{st.blocker_id}` ({len(st.behind)} card(s) parked): {st.reason or '(no reason)'}")
    if mine is not None:
        lines.append("")
        lines.append(
            f"This run is the probe for `{mine.blocker_id}`. Check first whether it still holds. If it "
            f"does, end again with `blocker_id: \"{mine.blocker_id}\"`. If it is gone, carry on: ending "
            "without that id clears it and releases the other parked cards.")
    lines.append("")
    return "\n".join(lines)
