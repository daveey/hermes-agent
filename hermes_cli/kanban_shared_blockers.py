"""Shared external blockers: park many cards behind ONE blocker and probe it once. [daveey]

A worker that hits a wall outside its card (CI billing refused, a provider outage, a release
pipeline down) ends its run with ``kanban_block(..., blocker_id="<slug>")`` or
``kanban_continue(..., blocker_id="<slug>")``. The id lands in that run's metadata
(``blocker_id``); nothing else is stored, so the blocker's state is derived from run history:

* a card is **behind** blocker X while its latest run that said anything about X carries X;
* X was **asserted** at the newest ``ended_at`` of any run carrying X;
* X was **cleared** only on evidence that the wall is gone, at the newest of:
  - the ``ended_at`` of a run that followed an X-run on the same card and completed or handed
    off (``CLEAR_OUTCOMES``);
  - the ``ended_at`` of such a run that ended with ``clear_blocker: true`` in its metadata
    (an explicit "the wall is gone", passed to kanban_block/kanban_schedule/kanban_continue);
  - the ``ended_at`` of such a run that named a different blocker (the probe got past X and
    hit another wall);
  - a ``condition_met`` event (the card's wake_check exited 0) on a card behind X, when the park
    that set that wake_check carried X (a probe set by a park without the id may test anything);
* X is **open** while asserted > cleared.

A re-park WITHOUT the id (``blocked``/``scheduled``/``continued`` with no ``clear_blocker``) says
nothing about X: the card stays behind X and X stays open. It used to count as a clear, so a
parent chat re-parking one card released every other card while the wall still stood
(2026-10-28, t_e7dc822c run 5878 released t_01a1800921b1ff50).

While X is open the dispatcher spawns at most one card behind it (the *probe*) per
``kanban.shared_blocker_probe_seconds``; the others are held with respawn guard reason
``shared_blocker`` instead of each re-discovering the same wall. A human unblock of a card lets
that card probe at once (still one probe at a time). Every card parked behind X keeps its own
wake (``kanban_timed_park``: a blocker park with no wake is given one after the probe interval),
so the probe still runs when no card behind X sits ``ready``. When X clears, held ``ready``
cards dispatch normally and cards still ``blocked`` or ``scheduled`` behind X are woken by
:func:`release_cleared_blockers`, once per clear: a card that was woken for it (or ran since)
and re-parked without the id is not woken again until a later clear.

Neutral outcomes (crash, timeout, reclaim, spawn failure, rate limit, give-up, failure) say
nothing about the external condition, so they neither assert nor clear a blocker.

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
PROBE_RELEASED_EVENT = "shared_blocker_probe_released"
DEFAULT_PROBE_SECONDS = 1800
MAX_BLOCKER_ID_CHARS = 64
# Outcomes that prove nothing about an external condition.
NEUTRAL_OUTCOMES = frozenset({
    "crashed", "oom_killed", "signaled", "timed_out", "reclaimed", "spawn_failed", "rate_limited", "gave_up", "released",
})
# Only blockers asserted this recently are considered (bounds the scan on old boards).
_LOOKBACK_SECONDS = 14 * 86400
# A run without the id that ended this way is evidence the wall is gone: the card finished,
# or handed off to review / back from review.
CLEAR_OUTCOMES = frozenset({"completed", "review_requested", "changes_requested"})
# Run metadata / tool arg: the caller says the card's shared blocker is gone.
CLEAR_FLAG = "clear_blocker"
CONDITION_MET_EVENT = "condition_met"   # kanban_wake_check.CONDITION_MET_EVENT


def run_effect(outcome: Optional[str], metadata) -> tuple[str, Optional[str]]:
    """What one closed run says about the card's shared blocker: ``("assert", id)``,
    ``("clear", None)`` (completed/handed off, or ``clear_blocker``), or ``("pass", None)``
    (a neutral outcome or a re-park without the id: says nothing). [daveey]"""
    from hermes_cli import kanban_db as kb

    meta = kb._json_dict(metadata)
    bid = normalize_blocker_id(meta.get("blocker_id"))
    if bid is not None:
        return "assert", bid
    if meta.get(CLEAR_FLAG) is True or (outcome or "") in CLEAR_OUTCOMES:
        return "clear", None
    return "pass", None

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
    cleared_by: Optional[tuple[str, Optional[int]]] = None   # (task_id, run_id or None) that cleared it
    cleared_via: str = ""                           # "run" or "condition_met"
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
        asserting_run: Optional[int] = None
        for run in runs:
            started = kb._to_epoch(run["started_at"]) or 0
            if current is not None and current[0] in states:
                st = states[current[0]]
                st.probe_starts[tid] = max(st.probe_starts.get(tid, 0), started)
            if run["ended_at"] is None:
                continue
            ended = kb._to_epoch(run["ended_at"]) or 0
            effect, bid = run_effect(run["outcome"], run["metadata"])
            if effect == "assert" and bid is not None:
                if current is not None and current[0] != bid and current[0] in states:
                    _mark_cleared(states[current[0]], tid, run["id"], ended)
                current, asserting_run = (bid, ended), int(run["id"])
                continue
            if effect == "pass":
                continue
            if current is not None and current[0] in states:
                _mark_cleared(states[current[0]], tid, run["id"], ended)
            current = None
        if current is not None and current[0] in states:
            # The card's own wake_check exiting 0 after it parked behind X is evidence too.
            # Ordered by event id after the asserting run's own events (same-second safe).
            met = conn.execute(
                "SELECT id, created_at, payload FROM task_events WHERE task_id = ? AND kind = ? AND id > COALESCE("
                "(SELECT MAX(id) FROM task_events WHERE task_id = ? AND run_id = ?), 0) "
                "ORDER BY id DESC LIMIT 1", (tid, CONDITION_MET_EVENT, tid, asserting_run),
            ).fetchone()
            if met is not None and _probe_parked_behind(conn, kb, tid, met, current[0]):
                _mark_cleared(states[current[0]], tid, None,
                              max(kb._to_epoch(met["created_at"]) or 0, current[1]), via=CONDITION_MET_EVENT)
        if current is not None and live and current[0] in states:
            states[current[0]].behind[tid] = current[1]
    for st in states.values():
        # Probe starts only count once the card is (still) behind the blocker.
        st.probe_starts = {t: s for t, s in st.probe_starts.items() if t in st.behind}
    return states


def _probe_parked_behind(conn: sqlite3.Connection, kb, task_id: str, met, blocker_id: str) -> bool:
    """True when the wake_check whose exit 0 is ``met`` was set by a park that carried
    ``blocker_id``. Every park replaces the card's probe (``kanban_wake_check.set_check``), so the
    probe that fired is the one from the newest parking event before ``met``. A probe set by a
    park WITHOUT the id may test anything (2026-10-28: ``ac8-live.sh || watch_eve.py`` exited 0
    on every Slack message), so its exit 0 is not evidence the wall is gone. [daveey]"""
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS

    marks = ",".join("?" * len(SCHEDULED_ENTRY_EVENTS))
    park = conn.execute(
        f"SELECT payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) AND id < ? "
        "ORDER BY id DESC LIMIT 1", (task_id, *SCHEDULED_ENTRY_EVENTS, int(met["id"])),
    ).fetchone()
    if park is None:
        return False
    park_payload = kb._json_dict(park["payload"])
    if normalize_blocker_id(park_payload.get("blocker_id")) != blocker_id:
        return False
    fired = kb._json_dict(met["payload"]).get("wake_check")
    return not fired or park_payload.get("wake_check") == fired


def _already_saw_clear(conn: sqlite3.Connection, kb, task_id: str, st: BlockerState) -> bool:
    """True when ``task_id`` already acted after ``st``'s newest clear: it was woken for it (a
    :data:`CLEARED_EVENT` for this blocker at or after ``cleared_at``) or a run of it started
    after it. Such a card re-parked without the id on purpose (it checked and the wall still
    stood for it); waking it again every tick looped forever (2026-10-28, t_01a180e8c9732286:
    one wasted run per park). A LATER clear still wakes it. [daveey]"""
    if st.probe_starts.get(task_id, 0) > st.cleared_at:
        return True
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 20", (task_id, CLEARED_EVENT, st.cleared_at),
    ):
        if normalize_blocker_id(kb._json_dict(row["payload"]).get("blocker_id")) == st.blocker_id:
            return True
    return False


def _mark_cleared(st: BlockerState, task_id: str, run_id: Optional[int], ended: int, *,
                  via: str = "run") -> None:
    if ended >= st.cleared_at:
        st.cleared_at = ended
        st.cleared_by = (task_id, int(run_id) if run_id is not None else None)
        st.cleared_via = via


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
        "SELECT id, payload FROM task_events WHERE kind IN ('blocked', 'scheduled', 'continued', 'block_loop_detected') "
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
    """Wake ``blocked`` and ``scheduled`` cards behind a blocker that later evidence cleared.
    Returns task ids."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    released: list[str] = []
    for st in blocker_states(conn, now=now).values():
        if st.is_open or st.cleared_by is None:
            continue
        by_task, by_run = st.cleared_by
        for tid, asserted in sorted(st.behind.items()):
            if asserted > st.cleared_at or tid == by_task:
                continue
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None or row["status"] not in ("blocked", "scheduled"):
                continue
            if _already_saw_clear(conn, kb, tid, st):
                continue
            payload = {"blocker_id": st.blocker_id, "cleared_by_task": by_task, "cleared_by_run": by_run,
                       "cleared_via": st.cleared_via or "run"}
            if row["status"] == "blocked":
                if not kb.unblock_task(conn, tid):
                    continue
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, CLEARED_EVENT, payload)
            else:
                with kb.write_txn(conn):
                    if kwc._wake(conn, tid, CLEARED_EVENT, payload) is None:
                        continue
            how = (f"{by_task}'s wake_check exited 0" if st.cleared_via == CONDITION_MET_EVENT
                   else f"{by_task} run {by_run} ended without hitting it")
            with kb.write_txn(conn):
                kb._insert_comment(
                    conn, tid, "dispatcher",
                    f"shared blocker `{st.blocker_id}` cleared: {how}, so this card was woken automatically.",
                    int(time.time()),
                )
            released.append(tid)
    return released


def probe_stranded_blockers(conn: sqlite3.Connection, *, now: Optional[int] = None,
                            interval: Optional[int] = None) -> list[str]:
    """Unblock ONE ``blocked`` card per open blocker that nothing else would probe. Returns task ids.

    :func:`shared_blocker_guard` only spaces out ``ready`` cards, so a blocker whose cards are all
    ``blocked`` (a lone card, or every card re-asserted) was never probed and never cleared: the
    cards sat forever (t_e7dc822c). When no card behind the blocker is ``ready`` or ``running`` and
    the probe interval has passed since the newest assertion or probe start, the card whose
    assertion is oldest is unblocked; it then spawns as the probe. Cards carrying a human ask
    (``human_request``) are left for the human. [daveey]"""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    if interval is None:
        interval = probe_interval_seconds()
    probed: list[str] = []
    for st in blocker_states(conn, now=now).values():
        if not st.is_open or not st.behind:
            continue
        ids = sorted(st.behind)
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, status, human_request FROM tasks WHERE id IN ({marks})", ids).fetchall()
        if any(r["status"] in ("ready", "running") for r in rows):
            continue   # the ready-card guard already spaces those probes
        last = max([st.asserted_at, *st.probe_starts.values()])
        if now - last < interval:
            continue
        candidates = sorted((st.behind[r["id"]], r["id"]) for r in rows
                            if r["status"] == "blocked" and not r["human_request"])
        for _asserted, tid in candidates:
            if not kb.unblock_task(conn, tid):
                continue
            with kb.write_txn(conn):
                kb._append_event(conn, tid, PROBE_RELEASED_EVENT, {
                    "blocker_id": st.blocker_id, "idle_seconds": now - last})
            probed.append(tid)
            break
    return probed


def render_blocker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Worker-context section naming open blockers so workers reuse the same id."""
    states = [s for s in blocker_states(conn).values() if s.is_open]
    if not states:
        return ""
    lines = ["## Open shared blockers on this board",
             "External walls other cards already hit. If yours is one of them, stop early and end "
             "with kanban_block/kanban_continue passing the same `blocker_id` instead of rediscovering "
             "it; only one card probes a blocker at a time. A timed wait (kind='time' or a "
             "SCHEDULED_UNTIL= reason) keeps its wake time and wake_check when it also passes "
             "blocker_id."]
    mine = None
    for st in sorted(states, key=lambda s: -s.asserted_at):
        if task_id in st.behind:
            mine = st
        lines.append(f"- `{st.blocker_id}` ({len(st.behind)} card(s) parked): {st.reason or '(no reason)'}")
    if mine is not None:
        lines.append("")
        lines.append(
            f"This run is the probe for `{mine.blocker_id}`. Check first whether it still holds. If it "
            f"does, end again with `blocker_id: \"{mine.blocker_id}\"`. If it is gone, carry on: "
            "completing or handing off clears it and wakes the other parked cards. A park without the "
            "id does NOT clear it; if you park for another reason and saw the wall gone, pass "
            "`clear_blocker: true`.")
    lines.append("")
    return "\n".join(lines)
