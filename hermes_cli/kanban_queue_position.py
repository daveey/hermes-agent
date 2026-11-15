"""Where a ``ready`` card stands in the queue that will start it, for task reads. [daveey]

A ready card with no claim for hours looked the same as a stuck one, so chats re-investigated queued cards
(three times in a day, 2026-11-02/03). The hub's own dispatcher claims ready cards by priority, then age
(``kanban_db_dispatch_queries.lane_rows``), under a host concurrency cap; a ``worker-first-v1`` card waits for the
worker pool instead, and the Board's last answer about it is stored by ``kanban_queue_reason``.

:func:`position` returns ``{"lane": "local", "ahead", "running", "limit", "approximate": True}`` or
``{"lane": "worker_pool", "reason": <code>}``; ``{"lane": "leftover_claim", "claim_lock", "worker_pid"}`` for a ready card
still holding a local claim (never dispatched); None for a card that is not ready.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)


def _ahead(conn: sqlite3.Connection, task) -> int:
    """Approximate combined-lane score position among unclaimed cards of this assignee.

    Worker-first cards wait for the pool, not a local slot. Other eligibility
    gates remain advisory here rather than running network-bearing preclaim checks.
    """
    from hermes_cli.kanban_admission_pools import lane_rows
    from hermes_cli.kanban_admission_score import rank
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    from hermes_cli.kanban_worker_placement import POLICY
    eligible = {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status IN ('ready', 'review') AND claim_lock IS NULL AND assignee IS ? "
        "AND (placement_policy IS NOT ? OR id = ?)",
        (task.assignee, POLICY, task.id),
    ).fetchall()}
    rows = list(lane_rows(conn, "ready"))
    if review_dispatch_enabled():
        rows.extend(lane_rows(conn, "review"))
    ids = [r["id"] for r, _parts in rank(conn, rows) if r["id"] in eligible]
    return ids.index(task.id) if task.id in ids else len(ids)


def _running_and_limit(conn: sqlite3.Connection, board: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """The host's running workers (every board) and its ``max_in_progress`` cap, as the dispatcher counts them.

    The board to exclude from the other-board sum is the one ``conn`` is attached to (``kanban_host_counts``),
    never the process's current board; ``board`` only labels a connection that matches no active board. An
    unreadable count is unknown (None), not zero or a partial sum."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_host_counts as khc
    running = khc.running_on_host(conn)["running"]
    try:
        limit = kbd.resolve_max_in_progress(kbd.configured_max_in_progress())
    except Exception:  # health: allow BLE001 -- an unreadable cap is unknown
        _log.debug("kanban queue position: cap unavailable", exc_info=True)
        limit = None
    return running, limit


def _runs_on(conn: sqlite3.Connection, task_id: str) -> Optional[list[str]]:
    try:
        row = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:  # an unsynced schema has no runs_on column: no placement labels
        return None
    from hermes_cli.kanban_worker_node import _parse
    return _parse(row[0]) if row is not None else None


# A drain flag older than this is not evidence: the machine stopped reporting, and a stale flag would hide that.
DRAIN_SAMPLE_MAX_AGE_S = 15 * 60


def draining(entry: Optional[dict[str, Any]], *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """``{}`` or ``{"until_ms": <announced bound>}`` when the machine's latest Board metrics sample (at most
    :data:`DRAIN_SAMPLE_MAX_AGE_S` old) says a core-update drain holds its dispatcher; None otherwise. [daveey]

    t_01a1cca8ed755919: the hub's dispatcher held every claim for a core-update drain from 13:49 to 14:14 PST
    (11-11). A MacBook card pinned ``machine:hub`` at 13:49:38 read "queued, not stuck" for 20 min, then was
    archived as lost. Each machine reports ``drain_active`` (``machine_drain_metrics``) while a drain-hold file
    exists, so the origin can say why instead."""
    import time
    metrics = (entry or {}).get("metrics") or {}
    at, until = metrics.get("at"), metrics.get("drain_until_ms")
    if metrics.get("drain_active") != 1 or not isinstance(at, (int, float)):
        return None
    if ((time.time() if now is None else now) * 1000 - at) > DRAIN_SAMPLE_MAX_AGE_S * 1000:
        return None
    return {"until_ms": int(until)} if isinstance(until, (int, float)) and until > 0 else {}


def placement(conn: sqlite3.Connection, task_id: str, *, backlog: bool = True) -> Optional[dict[str, Any]]:
    """For a card whose ``runs_on`` this machine does not satisfy, on a synced board with a readable machine
    registry: ``{"lane": "unplaceable", runs_on, missing, machines}`` when no known machine satisfies it (``missing``
    = labels no machine carries; empty when each exists but none carries them all), else ``{"lane": "machine",
    runs_on, machines}`` naming the machines that can lease it. None otherwise (no labels, sync off, registry
    unknown, or this machine fits): an unknown registry never reads as unplaceable. [daveey] t_01a1a521638d0a78
    (runs_on ["hermes-hub"], no machine carries it) sat ready for days reading as a local slot wait.
    Set ``backlog=False`` for the same placement verdict without ranking the label queue."""
    labels = _runs_on(conn, task_id)
    if not labels:
        return None
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as kwn
    db = sync._db_key(conn)
    if not sync.sync_enabled() or not db or not sync.board_selected(db):
        return None
    try:
        reg = kwn.registry()
    except Exception:  # health: allow BLE001 -- an unreadable registry is unknown, never "unplaceable"
        _log.debug("kanban queue position: machine registry unavailable", exc_info=True)
        return None
    machines = (reg or {}).get("machines") or {}
    me = (reg or {}).get("me")
    if not machines or me not in machines:
        return None
    carried = {name: kwn.effective_labels(entry, name) for name, entry in machines.items()}
    wanted = set(labels)
    if wanted <= carried[me]:
        return None
    fits = sorted(name for name, have in carried.items() if wanted <= have)
    if fits:
        place: dict[str, Any] = {"lane": "machine", "runs_on": labels, "machines": fits}
        held = [name for name in fits if (machines.get(name) or {}).get("hold")]
        if held:  # a held machine imports and leases nothing: the wait has no end until an operator releases it
            place["held"] = held
        drains = {name: d for name in fits if (d := draining(machines.get(name))) is not None}
        if drains:  # a draining machine's dispatcher imports and claims nothing until its drain ends
            place["draining"] = sorted(drains)
            until = [d["until_ms"] for d in drains.values() if "until_ms" in d]
            if until:
                place["drain_until_ms"] = max(until)
        if backlog:
            from hermes_cli import kanban_label_backlog as klb
            queue = klb.read(conn, task_id, labels, machines=klb.available(labels, machines))
            if queue.get("ahead") or klb.busy(queue):
                place["backlog"] = queue
        return place
    every = set().union(*carried.values())
    return {"lane": "unplaceable", "runs_on": labels, "missing": [lb for lb in labels if lb not in every],
            "machines": sorted(machines)}


def recorded_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Last dispatch hold in this ready episode; advisory, never a fresh verdict.

    Do not run the guard here: PR lookups can do network I/O, and notice callers
    hold a write transaction. A later lifecycle handoff invalidates the observation.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_diagnostics import _READY_WAIT_FLOOR_KINDS
    from hermes_cli.kanban_quota_episode_release import KIND as QUOTA_RELEASED

    kinds = {*_READY_WAIT_FLOOR_KINDS, "respawn_guarded", "continued", "scheduled", "reassigned", "reopened",
             "hold_released", "dispatch_skipped", QUOTA_RELEASED}  # a later dispatcher skip is the newer verdict
    slots = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ({slots}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, *sorted(kinds)),
    ).fetchone()
    if row is not None and row["kind"] == "respawn_guarded":
        reason = kb._json_dict(row["payload"]).get("reason")
        return reason if isinstance(reason, str) and reason else None
    return None


def position(conn: sqlite3.Connection, task, board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The queue a ``ready`` ``task`` in this gateway's live file waits in; None for any other status.

    A quota episode released since the card's last run (``quota_episode_released``) is named on the place, so
    a read says the quota hold is gone instead of echoing the old ``subscription_quota_reset``."""
    place = _position(conn, task, board)
    if place:
        from hermes_cli.kanban_quota_episode_release import covers_latest
        released = covers_latest(conn, task.id)
        if released:
            place["quota_released"] = {k: released.get(k) for k in ("reason", "via", "actor", "until")}
    return place


def _position(conn: sqlite3.Connection, task, board: Optional[str] = None) -> Optional[dict[str, Any]]:
    if task is None or task.status != "ready":
        return None
    # [daveey] A ready card still holding a local claim is never picked (the dispatcher claims only claim_lock IS
    # NULL); say so instead of a slot wait (agent-dc4177df2e: T6 read as a CPU wait for 5 days).
    lock = conn.execute("SELECT claim_lock, worker_pid FROM tasks WHERE id = ?", (task.id,)).fetchone()
    if lock is not None and lock[0]:
        return {"lane": "leftover_claim", "claim_lock": lock[0], "worker_pid": lock[1]}
    guard = recorded_guard(conn, task.id)
    if guard:
        return {"lane": "respawn_guard", "reason": guard}
    from hermes_cli.kanban_worker_placement import POLICY
    if getattr(task, "placement_policy", None) == POLICY:
        from hermes_cli import kanban_queue_reason as kqr
        try:
            value = kqr.latest(conn, task.id)
        except (sqlite3.Error, IndexError):  # an unsynced schema has no lease columns: no stored reason
            value = None
        return {"lane": "worker_pool", "reason": (value or {}).get("code")}
    placed = placement(conn, task.id)
    if placed is not None:
        return placed
    from hermes_cli import kanban_host_counts as khc
    running, limit = _running_and_limit(conn, board)
    from hermes_cli import kanban_board_share as share
    from hermes_cli.kanban_admission_pools import queue_fields
    try:
        elsewhere = share.ahead_elsewhere(conn, task.id)
    except Exception:  # health: allow BLE001 -- other boards are advisory; an unreadable count is unknown
        _log.debug("kanban queue position: other boards unavailable", exc_info=True)
        elsewhere = None
    place = {"lane": "local", "ahead": _ahead(conn, task), "running": running, "limit": limit, "approximate": True,
             "board": khc.board_of(conn) or board, "observed_at": int(time.time()),
             **queue_fields(limit, getattr(task, "admission_class", "normal"), conn, task.id)}
    if elsewhere:  # only when other boards' cards rank ahead: the single-board payload keeps its shape
        place["ahead_other_boards"] = elsewhere
    from hermes_cli import kanban_dispatch_skip as skips
    skip = skips.latest(conn, task.id)
    if skip:  # why the last tick passed it over (kanban-dd62aef001): no longer a bare slot wait [daveey]
        place["skip"] = skip
    pool = _pool_could_run(conn, task.id)
    if pool:
        place["pool_could_run"], place["task_id"] = pool, task.id
    return place


def _pool_could_run(conn: sqlite3.Connection, task_id: str) -> Optional[list[str]]:
    """Pool workers that could run this home-waiting card now (``kanban_placement_coordination.stranded``).

    [daveey] kanban-1663abf6ab: one held pool member made every access-label card home-only; 15 sat behind a
    full hub for up to 37 h while five workers carried the labels, and the note only said "waiting for a slot"."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_placement_coordination as coordination
    try:
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
                          "ORDER BY id LIMIT 1", (task_id,)).fetchone()
        reason = kb._json_dict(ev["payload"]).get("portable_reason") if ev is not None else None
        if reason != coordination.REASON:
            return None
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return (coordination.stranded(dict(row), placement.config(), reason) or None) if row is not None else None
    except Exception:  # health: allow BLE001 -- advisory; an unreadable registry or config is no verdict
        _log.debug("kanban queue position: pool check unavailable for %s", task_id, exc_info=True)
        return None


def ahead_words(place: dict[str, Any], template: str) -> str:
    """``template`` (with ``{n}``) filled with the total ahead, naming cards on other boards that share the host
    slots: without them a card read "0 ahead" while other boards' cards took every freed slot (2026-11-12)."""
    ahead = int(place.get("ahead") or 0)
    other = place.get("ahead_other_boards") or 0
    if not other:
        return template.format(n=ahead)
    from hermes_cli.kanban_board_share import OTHER_BOARDS
    return template.format(n=ahead + other) + f" ({other} on {OTHER_BOARDS})"


_NOT_STUCK = ("queued, not stuck: check placement and available capacity first; re-place if suitable. "
              "Do not run its work outside the dispatcher (two workers on one card). "
              "Only consider a bounded relative priority bump when below the lane median")


def _all_draining(place: dict[str, Any]) -> bool:
    return bool(place.get("draining")) and set(place["draining"]) >= set(place.get("machines") or [])


def headline(place: dict[str, Any]) -> str:
    """The wait itself for a ``machine`` lane, without advice: ``describe`` and the chat-wait notice share it."""
    machines, runs_on = ", ".join(place.get("machines") or []), ", ".join(place.get("runs_on") or [])
    if _all_draining(place):
        until = place.get("drain_until_ms")
        bound = (f"announced bound {_stamp_ms(until)}, not guaranteed" if until else "end time unknown")
        return (f"waiting for {machines} (runs_on {runs_on}), which is DRAINING for a core update (Board registry "
                f"metrics; {bound}): its dispatcher claims no new cards until the drain ends")
    words = f"waiting for {machines} (runs_on {runs_on}); this machine cannot lease it"
    if place.get("draining"):
        words += f" ({', '.join(place['draining'])} draining for a core update)"
    from hermes_cli import kanban_label_backlog as klb
    if backlog := klb.words(place.get("backlog")):
        words += f"; {backlog}"
    return words


def _stamp_ms(ms: Any) -> str:
    from datetime import UTC, datetime
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def describe(place: Optional[dict[str, Any]]) -> Optional[str]:
    """One line for card reads (``kanban_show`` ``queue_note``, ``hermes kanban show``) saying what the card waits
    for, so "ready, no run" never reads as stuck. [daveey] kanban-7d720d4ab8: a chat saw only a null
    ``queue_reason`` for a card waiting on a full hub, called it stuck and ran its work in a subagent."""
    words = _describe_lane(place)
    released = (place or {}).get("quota_released")
    if words and isinstance(released, dict):
        words = (f"quota hold released ({released.get('reason') or 'no reason'}, via {released.get('via')}); "
                 "the card is admissible again. " + words)
    return words


def _describe_lane(place: Optional[dict[str, Any]]) -> Optional[str]:
    if not place:
        return None
    lane = place.get("lane")
    if lane == "respawn_guard":
        return (f"last recorded dispatch hold: respawn guard ({place.get('reason')}). "
                "Check the current dispatcher verdict and resolve that guard; priority changes do not clear it")
    if lane == "leftover_claim":
        return (f"held by leftover claim {place.get('claim_lock')} (worker gone, pid {place.get('worker_pid')}): "
                "the dispatcher skips a ready card with a claim_lock. Its leftover-claim sweep clears a dead local "
                "claim on the next tick; if this line persists, the claim belongs to a live or foreign worker")
    if lane == "unplaceable":
        missing = place.get("missing") or []
        what = (f"label(s) {', '.join(missing)} carried by no machine" if missing
                else "no machine carries all of " + ", ".join(place.get("runs_on") or []))
        return (f"unplaceable: no machine can run it ({what}; known: {', '.join(place.get('machines') or [])}). "
                "Re-place it (`hermes kanban move <id> --runs-on <label> --reason ...`); waiting will not start it")
    if lane == "machine" and place.get("held") and set(place["held"]) >= set(place.get("machines") or []):
        held = ", ".join(place["held"])
        return (f"waiting for {held} (runs_on {', '.join(place.get('runs_on') or [])}), which is ON HOLD in the "
                "Board registry: it claims no cards until an operator releases the hold (`hermes-mail admin "
                f"machine-audit {place['held'][0]}` shows who set it and when). Not started by waiting: re-place it "
                "(`hermes kanban move <id> --runs-on <label> --reason ...`) or wait for the release; do not clear "
                "a hold to start a card")
    if lane == "machine" and _all_draining(place):
        return (headline(place) + ". Not lost and not refused: it starts when the drain ends. If it cannot wait, "
                "re-place it (`hermes kanban move <id> --runs-on <label> --reason ...`); do not re-create it, "
                "clear the drain, or run its work outside the dispatcher")
    if lane == "machine":
        from hermes_cli import kanban_label_backlog as klb
        if klb.busy(place.get("backlog")):
            return headline(place) + ". Queued behind same-label cards: " + klb.ADVICE
        return headline(place) + ". " + _NOT_STUCK
    if lane == "worker_pool":
        return f"waiting for a pool worker ({place.get('reason') or 'no reason recorded'}). " + _NOT_STUCK
    return local_words(place)


def local_words(place: dict[str, Any], *, now: Optional[float] = None) -> str:
    """``describe`` for the ``local`` lane: the recorded dispatcher skip first, when there is one. One ``now``
    (default ``time.time()``) feeds the re-place gate and the skip's words and advice, so an expired quota wall is
    not offered the re-place while still said to start "when that wall resets" (PR #1865 review). [daveey]"""
    from hermes_cli import kanban_dispatch_skip as skips
    now = time.time() if now is None else now
    skip = place.get("skip")
    pooled = bool(place.get("pool_could_run")) and skips.pool_replace_helps(skip, now=now)
    ahead = place.get("ahead")
    words = (ahead_words(place, "waiting for a local slot: ~{n} ahead") if ahead is not None
             else "waiting for a local slot")
    pools = place.get("admission_pools")
    if pools is not None:
        census = ", ".join(f"{name} {pools[name] if pools[name] is not None else '?'}/"
                           f"{pools[name + '_cap'] if pools[name + '_cap'] is not None else '?'}"
                           for name in ("regular", "priority"))
        scored = place.get("admission_score")
        if scored:
            from hermes_cli.kanban_admission_score import summary
            census += "; " + summary(scored)
        if (place.get("admission_class") not in ("capacity", "urgent") and pools["regular"] is not None
                and pools["regular_cap"] is not None and pools["regular"] >= pools["regular_cap"]):
            census += f"; regular pool full ({pools['regular']}/{pools['regular_cap']}), a normal card waits for it"
        words = f"{words}, {census}; class {place['admission_class']}"
        pool_advice = ("Queued, not stuck: check class/placement and available capacity; priority slots are "
                       "marked-only. Do not run its work outside the dispatcher")
        if pooled:  # a pool worker could start it now: the census must not hide that (#1821)
            pool_advice = pool_words(place)
        if not skip:
            return f"{words}. {pool_advice}"
        words = f"last dispatcher skip of this ready episode: {skips.words(skip, now=now)} ({words})"
        return words + ". " + _skip_advice(place, skip, pool_advice, pooled, now)
    if place.get("running") is not None and place.get("limit") is not None:
        words += f", {place['running']}/{place['limit']} running"
    elif place.get("limit") is not None and "running" in place:
        words += f", running count unknown (cap {place['limit']})"
    # One shape with the census branch: a recorded skip is framed first, then its advice. This branch used to
    # return the pool hint before the skip, dropping the skip reason (PR #1829 review). [daveey]
    pool_advice = pool_words(place) if pooled else _NOT_STUCK
    if not skip:
        return words + ". " + pool_advice
    words = f"last dispatcher skip of this ready episode: {skips.words(skip, now=now)} ({words})"
    return words + ". " + _skip_advice(place, skip, pool_advice, pooled, now)


def _skip_advice(place: dict[str, Any], skip: dict[str, Any], ordering_advice: str, pooled: bool,
                 now: Optional[float] = None) -> str:
    """The advice after a recorded skip, by the rule the chat notices follow (``kanban_parent_unclaimed._options``):
    an ordering skip takes the lane's own advice (the pool re-place when a pool worker could run it); any other skip
    keeps its own advice, after the re-place sentence when one applies. [daveey] PR #1835 review: ``kanban show``
    replaced that advice with the re-place while the notices kept both."""
    from hermes_cli import kanban_dispatch_skip as skips
    if skips.is_ordering(skip):
        return ordering_advice
    return (pool_words(place, advice=False) + ". " if pooled else "") + skips.sentence_advice(skip, now=now)


def pool_words(place: dict[str, Any], *, advice: bool = True) -> str:
    """The re-place sentence for a local-lane card that ``pool_could_run`` workers could start now. Shared by
    :func:`local_words` and the chat-wait notices (``kanban_parent_unclaimed._pool_sentence``), which dropped it: a
    chat told David to wait for hub slots while worker-1 could run the card (kanban-e7a33105a4). ``advice=False``
    leaves out the dispatcher warning for the notices, whose own advice already ends with it. [daveey]"""
    from hermes_cli.kanban_placement_coordination import REASON
    pool = place.get("pool_could_run") or []
    return (f"Home-only by a stale creation verdict ({REASON}): pool worker(s) {', '.join(pool)} now carry its "
            "access labels and have free slots. Re-place it (`hermes kanban move "
            f"{place.get('task_id') or '<id>'} --runs-on machine:{pool[0]} --reason \"pool attests its labels\"`) "
            "instead of waiting for a local slot" + ("; do not run its work outside the dispatcher" if advice else ""))


def read(conn: sqlite3.Connection, task, board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """:func:`position` for card reads (``kanban_show``, ``hermes kanban show``): advisory, never fails the read."""
    try:
        return position(conn, task, board)
    except Exception:  # health: allow BLE001 -- an advisory queue place must never fail the read
        _log.debug("kanban queue position: unavailable for %s", getattr(task, "id", None), exc_info=True)
        return None


def fields(place: Optional[dict[str, Any]]) -> dict[str, Any]:
    """``{"queue", "queue_note"}`` for a card-read payload; empty when the card waits in no queue."""
    return {"queue": place, "queue_note": describe(place)} if place else {}


def detail_fields(conn, task, board=None):
    """Additive card-read fields for every state; legacy queue shape is unchanged."""
    from hermes_cli import kanban_capacity_evidence as evidence
    out = fields(read(conn, task, board))
    try:
        capacity = evidence.read(conn, task, board=board)
    except Exception:  # health: allow BLE001 -- diagnostics cannot fail a card read
        capacity = evidence._unknown("observation_unavailable")
    observation = capacity.get("execution_observation")
    out.update(capacity_evidence=capacity,
               execution_observations=([observation] if observation else []) + _work_observations(conn, task, board))
    out["capacity_note"] = evidence.describe(capacity)
    return out


def _work_observations(conn, task, board):
    """Optional card evidence for a detail read: any adapter failure is an explicit unknown, never a failed read."""
    from dataclasses import asdict
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS, add_cards, card_scope, conn_path, failed_card
    from hermes_cli.kanban_execution_source import assemble, transaction
    row = {**asdict(task), "board": board or "default"}
    try:
        gateway, profile = card_scope(conn_path(conn))
        row["_execution_scope"] = {"gateway": gateway, "profile": profile, "board": row["board"]}
        with transaction(conn) as read_at:
            from hermes_cli.kanban_session_activity import _require_local_execution_authority
            _require_local_execution_authority(conn)
            sources = assemble(conn, [task.id], board=board or "default", gateway=gateway, profile=profile,
                               observed_at=read_at)
        row["_execution_source"] = sources.get(task.id)
        return add_cards([row])[0].get("execution_observations", [])
    except ADAPTER_ERRORS:
        _log.debug("kanban queue position: execution observation unavailable for %s", task.id, exc_info=True)
        try:
            return [failed_card(row)]
        except ADAPTER_ERRORS:
            return []


def print_field(place: Optional[dict[str, Any]], queue_reason: Any, field) -> None:
    """The text ``queue:`` line, unless the worker-pool ``queue_reason`` line already took it."""
    if place and not queue_reason:
        field("queue", fields(place)["queue_note"])
