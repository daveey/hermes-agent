"""Durable reasons for a ready/review card a dispatcher tick passed over without claiming it. [daveey]

Defect kanban-dd62aef001 (hub, 2026-11-10): t_01a1c7a0c8139180 (bb-tuner, pinned claude-opus-5-5/anthropic) sat
ready 33 min with free worker slots while another bb-tuner card was claimed. The card carried no ``claimed``,
``skipped_*`` or ``respawn_guarded`` event, so the chat-wait and parent-unclaimed notices fell back to "no worker
(1 card(s) ahead of it, 4/7 worker slots busy)" and suggested a priority bump. The bump did nothing; an agent
guessed a quota wall and parked the card. The tick logs had rotated, so the real skip site was lost.

* :func:`record_tick` (the dispatcher's post-result seam, ``kanban_capacity_evidence.observe_dispatch``) appends
  ONE ``dispatch_skipped {reason, detail, until?}`` event per (card, reason, detail) per ready episode for every
  row the tick passed over: ``skipped_ineligible``, ``capsule_refused``, ``no_progress_held``,
  ``per_profile_capped``, ``claim_deferred`` (Board lease refusals and this host's admission-gate refusals, told
  apart by the deferral's ``origin``), ``owner_down``, ``pinned_quota_wall``, ``llm_paced`` and ``review_provider_wall``.
  A (reason, detail) the episode already holds adds nothing, unless the newest row is a timed hold (``until``)
  it replaces (:func:`_supersedes_timed`); a new one appends. At most :data:`MAX_PER_EPISODE` per
  episode, so two dispatchers that disagree cannot grow a card's history without bound.
* :func:`pinned_quota_hold` holds a fresh card (no run yet) on the ready lane whose explicit pin's provider pool
  is quota-walled right now (``kanban_provider_walls.live_quota_wall``): its first worker would exit 75 into
  the wall. Reason ``pinned_quota_wall``, ``until`` = the reset.
* Flapping details (hub 2026-11-12: ``claim_deferred`` alternated ``capacity`` / ``hub_cpu_at_or_above_limit``
  tick to tick and filled the 16-row cap in 5 min) add nothing once seen: the dedupe is against EVERY skip of the
  episode, so an episode holds one row per distinct (reason, detail). The reason the latest tick saw lives in the
  machine-local, unsynced :data:`SEEN_TABLE` (one row per card: episode anchor, current skip, keys recorded), so
  :func:`latest` stays current after a flap or the cap without appending. The table also covers the pickup round
  trip that duplicated a row: a foreign card's copy is evicted once its outbox drains and re-imported from the
  replica, which may not have pulled the just-pushed skip back yet (``kanban_worker_node.evict``/``import_cards``);
  a key this host recorded in the episode is not appended again while the replica catches up.
* :func:`latest` is the current skip of the ready episode; ``kanban_queue_position``, the chat-wait and
  the parent-unclaimed notices name it (:func:`words`) and drop the priority advice unless it is about ordering
  (:func:`is_ordering`). A card ready :data:`UNRECORDED_AFTER_SECONDS` with free slots and no recorded reason
  reads :data:`UNRECORDED` (:func:`unrecorded`), never a priority bump.

Advisory, like every notice: the dispatcher's live verdict decides admission, and a failure here never stops a
tick.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

EVENT = "dispatch_skipped"
PINNED_QUOTA_WALL = "pinned_quota_wall"
# The LLM quota-pace governor held the card (``kanban.llm_pacing``): detail ``<provider> <paced|held>, ...``.
LLM_PACED = "llm_paced"
MAX_PER_EPISODE = 16
DETAIL_CHARS = 200
UNRECORDED_AFTER_SECONDS = 30 * 60
UNRECORDED = "dispatcher skip reason unrecorded"
SEEN_TABLE = "kanban_dispatch_skip_seen"
SEEN_REFRESH_SECONDS = 60
SEEN_RETENTION_SECONDS = 7 * 86400
SEEN_MAX_ROWS = 5000
# Events after which an earlier skip no longer describes the card (same set the recorded guard reads).
_EPISODE_KINDS = ("continued", "scheduled", "reassigned", "reopened", "hold_released", "respawn_guarded")


def _episode_kinds() -> set[str]:
    from hermes_cli.kanban_diagnostics import _READY_WAIT_FLOOR_KINDS
    return {*_READY_WAIT_FLOOR_KINDS, *_EPISODE_KINDS}


def episode_skips(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """``dispatch_skipped`` payloads of the current ready episode, newest first. A ``respawn_guarded`` event
    ends the run of skips it follows: the guard is then the newer verdict (``kanban_queue_position``)."""
    from hermes_cli import kanban_db as kb
    kinds = sorted({*_episode_kinds(), EVENT})
    slots = ",".join("?" for _ in kinds)
    out: list[dict[str, Any]] = []
    for kind, payload, created_at in conn.execute(
            f"SELECT kind, payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({slots}) "
            "ORDER BY created_at DESC, id DESC LIMIT ?", (task_id, *kinds, MAX_PER_EPISODE + 1)):
        if kind != EVENT:
            break
        out.append({**kb._json_dict(payload), "at": kb._to_epoch(created_at)})
    return out


def _anchor(conn: sqlite3.Connection, task_id: str) -> str:
    """The episode's identity: ``kind@created_at`` of its opening event, '' before any. Not the event id: a
    re-imported pickup copy renumbers ids, while kind and created_at travel unchanged."""
    from hermes_cli import kanban_db as kb
    kinds = sorted(_episode_kinds() - {EVENT})
    row = conn.execute(
        f"SELECT kind, created_at FROM task_events WHERE task_id = ? AND kind IN ({','.join('?' * len(kinds))}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, *kinds)).fetchone()
    return "" if row is None else f"{row[0]}@{kb._to_epoch(row[1])}"


def _key(reason: Any, detail: Any, until: Any) -> tuple[Any, Any, Any]:
    # A moved reset is news for a timed wall (the notice names it); other reasons key on reason + detail.
    from hermes_cli.kanban_review_admission import REASON as REVIEW_WALL
    return (reason, detail, until if reason in (PINNED_QUOTA_WALL, REVIEW_WALL) else None)


def _skip_key(skip: dict[str, Any]) -> tuple[Any, Any, Any]:
    return _key(skip.get("reason"), skip.get("detail"), skip.get("until"))


def _has_seen_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (SEEN_TABLE,)).fetchone() is not None


def _seen(conn: sqlite3.Connection, task_id: str, anchor: str) -> Optional[dict[str, Any]]:
    """This host's current-skip row for ``task_id`` when it belongs to episode ``anchor``; else None."""
    if not _has_seen_table(conn):
        return None
    row = conn.execute(f"SELECT anchor, payload, keys, since, seen_at FROM {SEEN_TABLE} WHERE task_id = ?",
                       (task_id,)).fetchone()
    if row is None or row[0] != anchor:
        return None
    try:
        payload, keys = json.loads(row[1]), {tuple(k) for k in json.loads(row[2])}
    except (TypeError, ValueError):
        return None
    return ({"payload": payload, "keys": keys, "since": int(row[3]), "seen_at": int(row[4])}
            if isinstance(payload, dict) else None)


def latest(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """The current skip of the ready episode (``reason``, ``detail``, ``until`` when known, ``at``): the one this
    host's ticks last saw, unless a skip this host never saw was recorded after that; else the newest row."""
    skips = episode_skips(conn, task_id)
    newest = skips[0] if skips and isinstance(skips[0].get("reason"), str) else None
    seen = _seen(conn, task_id, _anchor(conn, task_id))
    if seen is None:
        return newest
    if newest is not None and _skip_key(newest) not in seen["keys"] and (newest.get("at") or 0) > seen["seen_at"]:
        return newest  # another dispatcher's verdict, newer than this host's
    return {**seen["payload"], "at": seen["since"]}


def _settled(conn: sqlite3.Connection, task_id: str, key: tuple[Any, Any, Any]) -> bool:
    """Read-only precheck: ``key`` is already the card's current skip, confirmed within
    :data:`SEEN_REFRESH_SECONDS`, so the tick needs no write transaction. A miss is rechecked under the write lock."""
    seen = _seen(conn, task_id, _anchor(conn, task_id))
    if seen is not None:
        return _skip_key(seen["payload"]) == key and time.time() - seen["seen_at"] < SEEN_REFRESH_SECONDS
    skips = episode_skips(conn, task_id)
    return bool(skips) and _skip_key(skips[0]) == key


def _write_seen(conn: sqlite3.Connection, task_id: str, anchor: str, payload: dict[str, Any],
                keys: set[tuple[Any, Any, Any]], since: int) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {SEEN_TABLE} (task_id TEXT PRIMARY KEY, anchor TEXT NOT NULL, "
                 "payload TEXT NOT NULL, keys TEXT NOT NULL, since INTEGER NOT NULL, seen_at INTEGER NOT NULL)")
    conn.execute(
        f"INSERT INTO {SEEN_TABLE} (task_id, anchor, payload, keys, since, seen_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (task_id) DO UPDATE SET anchor = excluded.anchor, payload = excluded.payload, "
        "keys = excluded.keys, since = excluded.since, seen_at = excluded.seen_at",
        (task_id, anchor, json.dumps(payload, ensure_ascii=False), json.dumps(sorted(keys, key=repr)), since,
         int(time.time())))


def _supersedes_timed(skips: list[dict[str, Any]], key: tuple[Any, Any, Any], current: bool) -> bool:
    """A seen ``key`` replaces a newest row that carries ``until``: append it again. Readers of the event log
    (``kanban_diagnostics``) drop a timed hold once its ``until`` passes, so without a newer row a card the dispatcher
    still holds (paced -> held until T -> paced) read as stranded past T (PR #3058 review). Bounded: once the
    untimed key is newest again, a repeat of the timed key (already held) appends nothing, so a paced/held flap adds
    one row; :data:`MAX_PER_EPISODE` caps any other flap. Not when ``key`` is already this host's current skip: that
    is a replica that has not pulled this host's row back yet, not a change. [daveey]"""
    return bool(skips) and skips[0].get("until") is not None and _skip_key(skips[0]) != key and not current


def record(conn: sqlite3.Connection, task_id: str, reason: str, *, detail: Any = None,
           until: Optional[float] = None, extra: Optional[dict[str, Any]] = None) -> bool:
    """Append the skip unless the episode already holds the same (reason, detail); True when it appended. Either
    way it becomes the card's current skip (:func:`latest`)."""
    from hermes_cli import kanban_db as kb
    detail = None if detail is None else str(detail)[:DETAIL_CHARS]
    until = None if until is None else int(until)
    key = _key(reason, detail, until)
    if _settled(conn, task_id, key):
        return False
    payload: dict[str, Any] = {"reason": reason, "detail": detail, **(extra or {})}
    if until is not None:
        payload["until"] = until
    with kb.write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] not in ("ready", "review"):
            return False  # it left the lane this tick (no-progress park, claimed): nothing waits any more
        anchor, skips = _anchor(conn, task_id), episode_skips(conn, task_id)
        seen = _seen(conn, task_id, anchor)
        # Keys this host recorded count too: a pickup copy re-imported from a replica that has not pulled this
        # host's just-pushed skip back yet reads as missing it for a tick or two (hub 2026-11-12).
        keys = {_skip_key(s) for s in skips} | (seen["keys"] if seen else set())
        current = seen is not None and _skip_key(seen["payload"]) == key
        append = (key not in keys or _supersedes_timed(skips, key, current)) and len(skips) < MAX_PER_EPISODE
        if append:
            kb._append_event(conn, task_id, EVENT, payload)
            keys.add(key)
        if seen is None or not current or append or time.time() - seen["seen_at"] >= SEEN_REFRESH_SECONDS:
            since = seen["since"] if seen is not None and current else int(time.time())
            _write_seen(conn, task_id, anchor, payload, keys, since)
    return append


def _prune_seen(conn: sqlite3.Connection) -> None:
    """Drop current-skip rows unwritten for :data:`SEEN_RETENTION_SECONDS`, and the oldest past
    :data:`SEEN_MAX_ROWS`. A pruned card falls back to its newest event until its next recorded change."""
    if not _has_seen_table(conn):
        return
    floor = int(time.time()) - SEEN_RETENTION_SECONDS
    stale = conn.execute(f"SELECT 1 FROM {SEEN_TABLE} WHERE seen_at < ? LIMIT 1", (floor,)).fetchone()
    if not stale and conn.execute(f"SELECT count(*) FROM {SEEN_TABLE}").fetchone()[0] <= SEEN_MAX_ROWS:
        return
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        conn.execute(f"DELETE FROM {SEEN_TABLE} WHERE seen_at < ?", (floor,))
        conn.execute(f"DELETE FROM {SEEN_TABLE} WHERE task_id NOT IN (SELECT task_id FROM {SEEN_TABLE} "
                     "ORDER BY seen_at DESC LIMIT ?)", (SEEN_MAX_ROWS,))


def _tick_skips(result: Any) -> list[tuple[str, str, Any, Optional[float], dict[str, Any]]]:
    """``(task_id, reason, detail, until, extra)`` for every row ``result`` says the tick passed over."""
    out: list[tuple[str, str, Any, Optional[float], dict[str, Any]]] = []
    out += [(tid, "admission_deferred", "hub_claim_screen", None, {})
            for tid in getattr(result, "admission_deferred", ())]
    for name in ("skipped_ineligible", "capsule_refused", "no_progress_held", "claim_deferred"):
        # A claim deferral carries who set it (local gate vs Board); the skip words read it back (:func:`words`).
        out += [(tid, name, code, None, {"origin": code.origin} if getattr(code, "origin", None) else {})
                for tid, code in getattr(result, name, ())]
    out += [(tid, "per_profile_capped", who, None, {"running": n})
            for tid, who, n in getattr(result, "skipped_per_profile_capped", ())]
    out += [(tid, "owner_down", f"{who}:{code}", None, {}) for tid, who, code in getattr(result, "owner_down", ())]
    out += [(tid, PINNED_QUOTA_WALL, f"{model} ({provider})", until, {"model": model, "provider": provider})
            for tid, model, provider, until in getattr(result, "pinned_quota_held", ())]
    out += [(tid, LLM_PACED, detail, until, extra) for tid, detail, until, extra in getattr(result, LLM_PACED, ())]
    from hermes_cli.kanban_start_gate import SKIP_REASON, word  # a plugin held the start (kanban_start_gate)
    out += [(tid, SKIP_REASON, word(reason), None, {}) for tid, reason in getattr(result, "respawn_guarded", ())
            if word(reason)]
    from hermes_cli.kanban_review_admission import REASON as REVIEW_WALL
    out += [(tid, REVIEW_WALL, None, pair[1], {})
            for tid, pair in getattr(result, "clock_held", {}).items()
            if isinstance(pair, tuple) and len(pair) == 2 and pair[0] == REVIEW_WALL]
    return out


def record_tick(conn: sqlite3.Connection, result: Any, *, dry_run: bool = False,
                captured=None, machine=None) -> int:
    """Record every skip of one finished tick; returns how many events were appended. Never raises."""
    if dry_run or getattr(result, "skipped_locked", False) or getattr(result, "skipped_owned", False):
        return 0
    written = 0
    for tid, reason, detail, until, extra in _tick_skips(result):
        try:
            if captured is not None and machine is not None:
                from hermes_cli.kanban_nonrunning_transport import envelope
                value = envelope(conn, result, tid, captured=captured, machine=machine)
                if value is not None:
                    extra = {**extra, 'non_running_v1': value}
            written += record(conn, tid, reason, detail=detail, until=until, extra=extra)
        except Exception:  # health: allow BLE001 -- the skip already happened; its evidence is advisory
            _log.debug("kanban dispatch: skip record failed for %s (%s)", tid, reason, exc_info=True)
    try:
        _prune_seen(conn)
    except Exception:  # health: allow BLE001 -- pruning advisory evidence never stops a tick
        _log.debug("kanban dispatch: skip-seen prune failed", exc_info=True)
    return written


# --- fresh card on a quota-walled explicit pin --------------------------------------------------------------


def pinned_quota_hold(conn: sqlite3.Connection, task_id: str, *, lane: str = 'ready') -> Optional[tuple[str, str, float]]:
    """``(class, auto, reset)`` when every eligible class route is quota-walled.

    Legacy concrete selectors are historical evidence, never an admission gate.
    Existing run quota episodes retain their own admission checks.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_review_model import is_review_run
    task = kb.get_task(conn, task_id)
    if task is not None and is_review_run(conn, task, lane):
        return None  # reviewer provenance and candidate walls belong to review admission
    if task is not None:
        from hermes_cli.kanban_class_walls import class_wall_routes
        from hermes_cli.kanban_model_tier import resolve_model_tier
        from hermes_cli.kanban_model_neutral import routing_view
        neutral = routing_view(task)
        model, provider, _ = resolve_model_tier(conn, neutral, lane='ready')
        from hermes_cli.kanban_quota_episode import active
        # The resolved class is authority; diagnostic reasons (pattern, retry,
        # profile fallback, explicit/default) must not exempt its quota walls.
        if isinstance(model, str) and model.startswith('class:') and not active(conn, task_id):
            routes, known = class_wall_routes(neutral, model, now=time.time())
            if known and not routes:
                return model, provider or 'auto', min(known.values())
    return None


def hold_pinned_quota(conn: sqlite3.Connection, task_id: str, result: Any, *, lane: str) -> bool:
    """Pre-claim lane admission: True (recorded on ``result``) when :func:`pinned_quota_hold` holds the row."""
    if lane != "ready":
        return False
    held = pinned_quota_hold(conn, task_id)
    if held is None:
        return False
    result.pinned_quota_held.append((task_id, *held))
    result.clock_held[task_id] = (PINNED_QUOTA_WALL, int(held[2]))
    return True


# --- wording for queue places and notices ------------------------------------------------------------------


def _stamp(epoch: Any, now: Optional[float] = None) -> str:
    try:
        when = time.gmtime(int(epoch))
    except (TypeError, ValueError, OverflowError, OSError):
        return "an unknown time"
    today = time.gmtime(time.time() if now is None else now)
    return time.strftime("%H:%MZ" if when[:3] == today[:3] else "%Y-%m-%d %H:%MZ", when)


_WORDS = {
    "admission_deferred": "held: hub claim screen deferred this card; retry next tick",
    PINNED_QUOTA_WALL: "held: pinned model {detail} quota-walled",
    LLM_PACED: "held: LLM quota pacing ({detail} pool)",
    "review_provider_wall": "held: independent review routes walled on this host",
    "start_gate": "held: a plugin start gate holds card starts ({detail})",
    "per_profile_capped": "skipped: assignee {detail} is at its per-profile worker cap",
    "skipped_ineligible": "skipped: this machine cannot lease it ({detail})",
    "capsule_refused": "skipped: capsule admission refused it ({detail})",
    "no_progress_held": "held: run budget spent ({detail})",  # no-progress or spend budget [daveey]
    "claim_deferred": "skipped: the Board deferred its claim ({detail})",
    "owner_down": "held: the assignee's gateway service is down ({detail})",
}


def _local_gate(skip: dict[str, Any]) -> bool:
    """The ``claim_deferred`` skip was set by this host (its admission gate, placement or worker-capacity check),
    not by the Board service: the producer stamps ``origin`` (``kanban_db_sync.ClaimDeferral``); no name list."""
    from hermes_cli.kanban_db_sync import ORIGIN_LOCAL
    return skip.get("origin") == ORIGIN_LOCAL


def words(skip: dict[str, Any], *, now: Optional[float] = None) -> str:
    """The skip as one clause, e.g. ``held: pinned model claude-opus-5-5 (anthropic) quota-walled until 01:59Z``."""
    reason = str(skip.get("reason") or "unknown")
    if reason == "llm_pacing":
        reason = LLM_PACED
    detail = skip.get("detail") or "no detail"
    text = _WORDS.get(reason, "skipped: {reason} ({detail})").format(reason=reason, detail=detail)
    if reason == PINNED_QUOTA_WALL and str(detail).startswith('class:'):
        text = f"held: eligible class routes {detail} quota-walled"
    if reason == "claim_deferred" and _local_gate(skip):
        text = f"skipped: this host's admission gate deferred its claim ({detail})"
    if skip.get("until"):
        text += f" until {_stamp(skip['until'], now)}"
        if _wall_passed(skip, now):
            text += " (reset passed)"
    return text


def is_ordering(skip: Optional[dict[str, Any]]) -> bool:
    """True when the skip is about queue order or slots, where priority is a fair question: a Board capacity
    deferral, or the assignee's per-profile cap (a slot wait among that profile's own cards)."""
    return bool(skip) and ((skip.get("reason") == "claim_deferred" and skip.get("detail") == "capacity")
                           or skip.get("reason") == "per_profile_capped")


# The details a skip carries when it says only that the machine which recorded it cannot lease the card: the Board's
# eligibility refusals for that machine (``kanban_worker_node.eligible``) and its own unreachable Board. A tuple, not
# a set: a synced payload's detail may be any JSON value.
_RECORDER_ONLY = ("labels_mismatch", "pinned_only", "held", "offline", "not_claiming")


def recorder_cannot_lease(skip: dict[str, Any]) -> bool:
    """True when ``skip`` says only that the machine which recorded it cannot lease the card. It is no verdict on
    the machines that can, so a notice may still name what holds them (``kanban_parent_unclaimed``,
    t_01a1db9d9538716b); any other skip came from a dispatcher able to lease the card, and its verdict stands."""
    return skip.get("reason") in ("skipped_ineligible", "claim_deferred") and skip.get("detail") in _RECORDER_ONLY


def advice(skip: dict[str, Any], *, now: Optional[float] = None) -> str:
    """What to do about a non-ordering skip; never a priority change. A quota wall whose ``until`` has passed by
    ``now`` (default ``time.time()``) drops "starts on its own when that wall resets": :func:`pool_replace_helps`
    offers the pool re-place for it at the same ``now`` (PR #1865 review). [daveey]"""
    if _wall_passed(skip, now):
        return ("that wall's reset has passed; the next dispatcher tick re-checks eligible routes. Priority changes do not "
                "clear a quota wall. Do not run its work outside the dispatcher")
    if skip.get("reason") == LLM_PACED:
        return ("it starts on its own once its provider pool is back on pace or a paced slot frees; priority changes "
                "do not lift a pacing hold. Do not run its work outside the dispatcher")
    if skip.get("reason") == PINNED_QUOTA_WALL:
        return ("it starts on its own when an eligible class route becomes available; priority changes do not clear "
                "a quota wall. Do not run its work outside the dispatcher")
    if skip.get("reason") == "review_provider_wall":
        return ("it starts on its own when an independent reviewer is available on this host; priority changes do not "
                "clear a reviewer wall. Do not run its work outside the dispatcher")
    return ("resolve that dispatcher skip reason; priority changes do not clear it. "
            "Do not run its work outside the dispatcher")


def sentence_advice(skip: dict[str, Any], *, now: Optional[float] = None) -> str:
    """:func:`advice` as a sentence of its own, for a join after a full stop (``kanban show``); the notices keep the
    lowercase clause after their ``NO_FIX: `` prefix. [daveey] PR #1846 review nit."""
    text = advice(skip, now=now)
    return text[:1].upper() + text[1:]


def _wall_lasts(skip: dict[str, Any], now: Optional[float]) -> bool:
    """True unless the skip's ``until`` is a readable time at or before ``now``; missing or unparseable keeps the
    wall (conservative)."""
    try:
        until = float(skip["until"])
    except (KeyError, TypeError, ValueError):
        return True
    return until != until or until > (time.time() if now is None else now)  # NaN is unparseable too


def _wall_passed(skip: dict[str, Any], now: Optional[float]) -> bool:
    """A ``pinned_quota_wall`` skip whose readable ``until`` is at or before ``now``: the exact complement of the
    :func:`pool_replace_helps` suppression, so the wording and the re-place offer agree."""
    return skip.get("reason") == PINNED_QUOTA_WALL and not _wall_lasts(skip, now)


def pool_replace_helps(skip: Optional[dict[str, Any]], *, now: Optional[float] = None) -> bool:
    """False when a pool re-place cannot clear ``skip``, so no surface may offer one. [daveey] PR #1835 review: a
    ``pinned_quota_wall`` hold follows the pin to any machine, and ``_pool_could_run`` never reads the pool workers'
    quota; its own advice (wait for the reset, or re-pin) stands. Any other skip's advice is kept after the
    re-place sentence (``kanban_queue_position.local_words`` and the notices' ``_options`` alike). A wall whose
    ``until`` has passed suppresses nothing, though no later tick has replaced the skip yet (PR #1846 review)."""
    return not skip or skip.get("reason") != PINNED_QUOTA_WALL or not _wall_lasts(skip, now)


UNRECORDED_FACT = f"{UNRECORDED}: slots are free, yet no tick recorded why it passed this card over."
UNRECORDED_ADVICE = (f"{UNRECORDED_FACT} Read the live verdict (`hermes kanban dispatch --dry-run --json`) before "
                     "changing anything; priority changes are not the fix. Do not run its work outside the dispatcher")
# After a pool re-place hint ("Re-place it ..."), "read the verdict before changing anything" contradicts the named
# fix, so a notice carrying ``pool_hint`` keeps only the fact and the dispatcher warning (PR #1829 review). [daveey]
UNRECORDED_POOL_ADVICE = f"{UNRECORDED_FACT} Do not run its work outside the dispatcher"


def unrecorded(place: Optional[dict[str, Any]], ready_seconds: Optional[float]) -> bool:
    """A local-lane card ready :data:`UNRECORDED_AFTER_SECONDS` or more, with free slots, and no recorded skip."""
    place = place or {}
    if place.get("lane") != "local" or place.get("skip") or ready_seconds is None:
        return False
    running, limit = place.get("running"), place.get("limit")
    return (ready_seconds >= UNRECORDED_AFTER_SECONDS and isinstance(running, int) and isinstance(limit, int)
            and running < limit)


def notice_fields(place: Optional[dict[str, Any]], ready_seconds: Optional[float]) -> dict[str, Any]:
    """Payload fields for a ready-wait notice: ``dispatch_skip`` or ``skip_unrecorded``; empty otherwise."""
    skip = (place or {}).get("skip")
    if skip:
        return {"dispatch_skip": {k: skip.get(k) for k in ("reason", "detail", "until", "origin")
                                  if skip.get(k) is not None}}
    return {"skip_unrecorded": True} if unrecorded(place, ready_seconds) else {}


def notice_advice(payload: dict[str, Any], *, now: Optional[float] = None) -> Optional[str]:
    """The advice for a notice payload carrying :func:`notice_fields`, or None for the ordinary capacity advice.
    ``now`` defaults to the payload's ``observed_at`` (when its queue words and pool hint were written), else the
    current time."""
    skip = payload.get("dispatch_skip")
    if isinstance(skip, dict) and skip and not is_ordering(skip):
        seen = payload.get("observed_at")
        if now is None and isinstance(seen, (int, float)) and not isinstance(seen, bool):
            now = seen
        return advice(skip, now=now)
    if not payload.get("skip_unrecorded"):
        return None
    return UNRECORDED_POOL_ADVICE if str(payload.get("pool_hint") or "").strip() else UNRECORDED_ADVICE
