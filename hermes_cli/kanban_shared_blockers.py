"""Shared external blockers: park many cards behind ONE blocker and probe it once. [daveey]

A worker that hits a wall outside its card (CI billing refused, a provider outage, a release
pipeline down) ends its run with ``kanban_block(..., blocker_id="<slug>")`` or
``kanban_continue(..., blocker_id="<slug>")``. The id lands in that run's metadata
(``blocker_id``); nothing else is stored, so the blocker's state is derived from run history:

* a card is **behind** blocker X while its latest run that said anything about X carries X;
* X was **asserted** at the newest ``ended_at`` of an asserting run carrying X;
  completed/handed-off runs never assert, even if their metadata carries X;
* X was **cleared** only on evidence that the wall is gone, at the newest of:
  - the ``ended_at`` of a run that followed an X-run on the same card and completed or handed
    off (``CLEAR_OUTCOMES``);
  - the ``ended_at`` of such a run that ended with ``clear_blocker: true`` in its metadata
    (an explicit "the wall is gone", passed to kanban_block/kanban_schedule/kanban_continue);
  - the ``ended_at`` of such a run that named a different blocker (the probe got past X and
    hit another wall);
  - a ``condition_met`` event (the card's wake_check exited 0) on a card behind X, when the park
    that set that wake_check carried X (a probe set by a park without the id may test anything);
* X is **open** while asserted > cleared and at least one live card is behind it.
  Done/archived cards cannot keep a wall open; a later encounter asserts it again.

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

Optional ``blocker_provider`` scopes a wall to that provider; ``blocker_model`` and
``blocker_lane`` describe the observation only. Other-provider steps neither wait nor act as
probes/clear evidence. Routing unknown (including unallocated classes) remains conservative;
unscoped history keeps its board-wide behavior. A run's route is its admission receipt
(``run_route`` event, written by ``kanban_model_tier.apply_model_tier`` on every spawn; legacy
runs fall back to their ``model_tier`` event), never a card's later pin or today's tier
configuration. Under a scoped blocker a run whose route is unknown (no receipt, or a
class/``auto`` receipt) is no implicit evidence: its completion neither clears the blocker nor
counts as a probe (an explicit ``clear_blocker`` attestation clears it on any route). A
card's unknown NEXT route stays conservative: the guard still holds it. The scope is the union
over every assertion of the id in the lookback window: one assertion without a provider, or
with a different one, keeps the id board-wide (a narrower wall needs a new id).

Matching on normalized error text was considered and left out: crash/timeout/protocol-violation
errors share fixed preambles across unrelated cards (``_error_fingerprint`` keeps 80 chars), and
LLM-written block reasons never match verbatim. Same-tick identical crash waves are already
tripped by ``_account_crashes``.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)
GUARD_REASON = "shared_blocker"
CLEARED_EVENT = "shared_blocker_cleared"
PROBE_RELEASED_EVENT = "shared_blocker_probe_released"
DEFAULT_PROBE_SECONDS = 1800
# [daveey] G2 follow-up: which cards a provider-scoped blocker holds comes from ``task_route``, whose inputs sit
# outside the probe lane's gate key: the assignee profile's config, the effective user/managed config, provider
# walls and quota episodes that end with time. A route can enter the scope with no board write, so while a scoped
# blocker is open the lane rechecks on this cadence instead of waiting for the next content write.
ROUTE_RECHECK_SECONDS = 300
MAX_BLOCKER_ID_CHARS = 64
# Outcomes that prove nothing about an external condition.
NEUTRAL_OUTCOMES = frozenset({
    "crashed", "oom_killed", "signaled", "timed_out", "reclaimed", "spawn_failed", "rate_limited", "gave_up", "released",
})
# Only blockers asserted this recently are considered (bounds the scan on old boards).
_LOOKBACK_SECONDS = 14 * 86400
# A run that ended this way is evidence the wall is gone: the card finished,
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

    if (outcome or "") in CLEAR_OUTCOMES:
        return "clear", None
    # [daveey] G2: most runs carry neither key; skip their JSON decode (the blocker lanes walk ~750 run histories
    # a pass on the hub board). Writers use json.dumps, which never escapes an ASCII key name.
    if isinstance(metadata, str) and '"blocker_id"' not in metadata and f'"{CLEAR_FLAG}"' not in metadata:
        return "pass", None
    meta = kb._json_dict(metadata)
    bid = normalize_blocker_id(meta.get("blocker_id"))
    if bid is not None:
        return "assert", bid
    if meta.get(CLEAR_FLAG) is True:
        return "clear", None
    return "pass", None

_ID_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_blocker_id(raw, *, cfg=None) -> Optional[str]:
    """Canonical slug; configured aliases augment (never split) built-in families."""
    if not isinstance(raw, str):
        return None
    slug = _ID_RE.sub("-", raw.strip().lower()).strip("-._")[:MAX_BLOCKER_ID_CHARS]
    if re.fullmatch(r"github(-(shared|rest|core))*-quota(-reserve)?", slug):
        return "github-rest-quota"
    if cfg is None:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config_backend import ConfigBackendUnavailable
        try:
            cfg = load_config_readonly()
        except ConfigBackendUnavailable:
            return slug or None
    aliases = (cfg.get("kanban") or {}).get("shared_blocker_aliases", {})
    target = aliases.get(slug) if isinstance(aliases, dict) else None
    if isinstance(target, str) and target.strip():
        # One hop only: cycles cannot recurse; targets must be canonical ids.
        return normalize_blocker_id(target, cfg={})
    return slug or None


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
    blocker_provider: Optional[str] = None
    blocker_model: Optional[str] = None
    blocker_lane: Optional[str] = None
    asserted_at: int = 0
    cleared_at: int = 0
    cleared_by: Optional[tuple[str, Optional[int]]] = None   # (task_id, run_id or None) that cleared it
    cleared_via: str = ""                           # "run" or "condition_met"
    # Earliest clear after the newest assertion. A card woken for it has seen every later clear
    # until X is re-asserted; ``cleared_at`` (the newest) is only for reporting. [daveey]
    first_cleared_at: int = 0
    reason: str = ""                                # summary of the newest asserting run
    # task_id -> ended_at of the asserting run the card is still behind
    behind: dict[str, int] = field(default_factory=dict)
    # task_id -> started_at of the newest run started after the card's asserting run
    probe_starts: dict[str, int] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return bool(self.behind) and self.asserted_at > self.cleared_at


def _optional_text(value) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _provider(value) -> Optional[str]:
    value = _optional_text(value)
    if not value or value.lower() in {"auto", "unknown"}:
        return None
    from hermes_cli.kanban_pin_provider import _canon
    return _canon(value)


def _applies(st: BlockerState, provider: Optional[str]) -> bool:
    # Model/lane describe the assertion, not restrictions. Unknown routing is conservative.
    scope, target = _provider(st.blocker_provider), _provider(provider)
    return not scope or not target or scope == target


def _run_receipt(conn, task_id, run_id) -> Optional[tuple[Optional[str], Optional[str], str]]:
    """The run's admission receipt, or None when it has none (a run admitted before receipts).

    Prefers the ``run_route`` event (every spawn, tier on or off); a legacy run falls back to its
    ``model_tier`` event. A receipt whose provider is unknown (class, ``auto``) keeps it None."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_tier import RUN_ROUTE_EVENT
    for kind in (RUN_ROUTE_EVENT, "model_tier"):
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1", (task_id, run_id, kind)).fetchone()
        if row is None:
            continue
        payload = kb._json_dict(row[0])
        model = _optional_text(payload.get("model"))
        provider = _provider(payload.get("provider"))
        if model and model.startswith("class:"):
            provider = None
        return model, provider, payload.get("lane") or kb._retry_status_for_run(conn, task_id, run_id)
    return None


def _run_route(conn, task_id, run_id) -> tuple[Optional[str], Optional[str], str]:
    """Historical routing evidence, never today's tier config or a later card pin. Unknown is None."""
    from hermes_cli import kanban_db as kb
    receipt = _run_receipt(conn, task_id, run_id)
    if receipt is not None:
        return receipt
    return None, None, kb._retry_status_for_run(conn, task_id, run_id)


def task_route(conn, task_id) -> tuple[Optional[str], Optional[str], str]:
    """Read-only preview of this run/next lane using the dispatcher's routing helpers.

    Class allocation stays in the worker: auto is unknown, never a guessed provider.
    """
    from hermes_cli import kanban_db as kb, kanban_model_tier as tier
    task = kb.get_task(conn, task_id)
    if task is None:
        return None, None, "ready"
    lane = task.status if task.status in {"ready", "review"} else kb._retry_status_for_run(conn, task_id)
    if task.status == "scheduled":
        from hermes_cli.kanban_continuation import pending_wake
        wake = pending_wake(conn, task_id) or {}
        lane = "review" if wake.get("resume_status") == "review" else "ready"
    if task.status == "running" and task.current_run_id:
        # A receipt is authoritative even when it says "unknown"; only a run admitted before
        # receipts falls back to the read-only preview below.
        receipt = _run_receipt(conn, task_id, task.current_run_id)
        if receipt is not None:
            return receipt
    try:
        from hermes_cli.kanban_model_assignment import _assignee_config
        from hermes_cli.kanban_pin_provider import resolve_legacy_pin
        cfg = tier.load_tier_config()
        model, provider, reason = tier.resolve_dispatch_tier(conn, task, lane=lane, cfg=cfg)
        chosen = bool(model)
        if reason == "pinned" and not task.provider_override:
            task.provider_override = resolve_legacy_pin(task)[0]
        if model:
            task.model_override, task.provider_override = model, provider
        model, provider = task.model_override, task.provider_override
        if model and model.startswith("class:"):
            if chosen and tier.class_unroutable_reason(model):
                model = provider = None
            else:
                return model, None, lane
        if not model:
            config = _assignee_config(task.assignee) or {}
            block = config.get("model") or {}
            if isinstance(block, dict):
                model, provider = block.get("default"), block.get("provider")
        if model and model.startswith("class:"):
            provider = None
        return _optional_text(model), _provider(provider), lane
    except Exception:  # route preview is best-effort: unknown routing stays conservative
        logger.debug("kanban shared blockers: route preview failed for %s", task_id, exc_info=True)
        return None, None, lane


def task_applies(conn, st: BlockerState, task_id: str) -> bool:
    """Whether the blocker holds this card's next/current step: unknown routing is conservative. The one rule for
    which cards behind ``st`` count, shared by :func:`shared_blocker_guard`, :func:`probe_stranded_blockers` and the
    CI sweep (``kanban_ci_progress``)."""
    return not st.blocker_provider or _applies(st, task_route(conn, task_id)[1])


def _run_is_evidence(st: BlockerState, provider: Optional[str]) -> bool:
    """Whether a run on ``provider`` says anything about ``st``: always for an unscoped blocker;
    for a scoped one only a KNOWN matching provider (unknown is never a clear or a probe)."""
    if not st.blocker_provider:
        return True
    return provider is not None and _applies(st, provider)


def _explicit_clear(kb, metadata) -> bool:
    """A run carrying ``clear_blocker`` is the caller's explicit attestation that the wall is gone
    (the documented clear path), so it clears a scoped blocker whatever the run's route: e.g. the
    zero-duration run synthesized when a breaker-parked untyped card is classified in place has
    no admission receipt. Only implicit evidence (a completion/handoff) needs a matching route."""
    return kb._json_dict(metadata).get(CLEAR_FLAG) is True


def _note_window_exit(out: Optional[list], asserting) -> None:
    """G2: when the oldest in-window assertion leaves the lookback, ``blocker_states`` can change unwritten."""
    from hermes_cli import kanban_db as kb

    if out is not None and asserting:
        out.append(min(kb._to_epoch(r["ended_at"]) or 0 for r in asserting) + _LOOKBACK_SECONDS + 1)


def blocker_states(conn: sqlite3.Connection, *, now: Optional[int] = None,
                   oldest_out: Optional[list] = None) -> dict[str, BlockerState]:
    """Derive every recently asserted blocker's state from ``task_runs`` (read-only).

    ``oldest_out`` (G2) receives the moment the oldest assertion inside the lookback window leaves it: then
    this derivation can change with no row changing."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    asserting = conn.execute(
        "SELECT id, task_id, started_at, ended_at, outcome, metadata, summary FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at >= ? AND metadata LIKE '%\"blocker_id\"%' "
        "ORDER BY id",
        (now - _LOOKBACK_SECONDS,),
    ).fetchall()
    _note_window_exit(oldest_out, asserting)
    states: dict[str, BlockerState] = {}
    scopes: dict[str, set[Optional[str]]] = {}
    task_ids: set[str] = set()
    for row in asserting:
        effect, bid = run_effect(row["outcome"], row["metadata"])
        if effect != "assert" or bid is None:
            continue
        st = states.setdefault(bid, BlockerState(bid))
        ended = kb._to_epoch(row["ended_at"]) or 0
        meta = kb._json_dict(row["metadata"])
        scopes.setdefault(bid, set()).add(_provider(meta.get("blocker_provider")))
        if ended >= st.asserted_at:
            st.asserted_at = ended
            st.reason = kb._first_line(row["summary"], 300)
            st.blocker_model = _optional_text(meta.get("blocker_model"))
            st.blocker_lane = _optional_text(meta.get("blocker_lane"))
        task_ids.add(row["task_id"])
    if not states:
        return states
    for bid, st in states.items():
        # Union of every recent assertion: any unscoped or differing provider keeps it board-wide.
        providers = scopes.get(bid, set())
        st.blocker_provider = next(iter(providers)) if len(providers) == 1 else None
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
            applicable = True
            if current is not None and current[0] in states:
                st = states[current[0]]
                provider = None
                if st.blocker_provider:
                    provider = (_run_route(conn, tid, run["id"])[1] if run["ended_at"] is not None
                                else task_route(conn, tid)[1])
                applicable = _run_is_evidence(st, provider)
                if applicable:
                    st.probe_starts[tid] = max(st.probe_starts.get(tid, 0), started)
            if run["ended_at"] is None:
                continue
            ended = kb._to_epoch(run["ended_at"]) or 0
            effect, bid = run_effect(run["outcome"], run["metadata"])
            if effect == "assert" and bid is not None:
                if applicable and current is not None and current[0] != bid and current[0] in states:
                    _mark_cleared(states[current[0]], tid, run["id"], ended)
                current, asserting_run = (bid, ended), int(run["id"])
                continue
            if effect == "pass" or not (applicable or _explicit_clear(kb, run["metadata"])):
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
    from hermes_cli.kanban_shared_blocker_checks import apply_check_clears
    apply_check_clears(conn, states, now=now)
    from hermes_cli.kanban_shared_blocker_fence import fence_stale_quota_assertions
    fence_stale_quota_assertions(conn, states, asserting, now=now)
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
    """True when ``task_id`` already acted after ``st``'s FIRST clear since its newest assertion:
    it was woken for it (a :data:`CLEARED_EVENT` for this blocker at or after ``first_cleared_at``)
    or a run of it started after it. Such a card re-parked without the id on purpose (it checked
    and the wall still stood for it); waking it again every tick looped forever (2026-10-28,
    t_01a180e8c9732286: one wasted run per park). Measuring against the first clear, not the
    newest, makes a clear idempotent: a second card clearing X again with no re-assertion in
    between is no new information (2026-10-28 15:53, t_e7dc822c woken twice). A later clear wakes
    the card again only after X was re-asserted. [daveey]"""
    since = st.first_cleared_at or st.cleared_at
    if st.probe_starts.get(task_id, 0) > since:
        return True
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 20", (task_id, CLEARED_EVENT, since),
    ):
        if normalize_blocker_id(kb._json_dict(row["payload"]).get("blocker_id")) == st.blocker_id:
            return True
    return False


def _mark_cleared(st: BlockerState, task_id: str, run_id: Optional[int], ended: int, *,
                  via: str = "run") -> None:
    if ended > st.asserted_at and (not st.first_cleared_at or ended < st.first_cleared_at):
        st.first_cleared_at = ended
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

    unblocked = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='unblocked'", (task_id,)
    ).fetchone()[0]
    if unblocked is None:
        return False
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE kind IN ('blocked', 'scheduled', 'continued', 'block_loop_detected') "
        "AND id>=? AND payload LIKE '%\"blocker_id\"%' ORDER BY id DESC", (unblocked,),
    ):
        if normalize_blocker_id(kb._json_dict(row["payload"]).get("blocker_id")) == blocker_id:
            return False
    return True


def shared_blocker_guard(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
    interval: Optional[int] = None,
) -> Optional[str]:
    """:data:`GUARD_REASON` when ``task_id`` sits behind an open blocker and must wait for the
    probe; None when it may spawn (no blocker, blocker cleared, or this card is the probe)."""
    now = int(time.time()) if now is None else int(now)
    states = blocker_states(conn, now=now)
    st = blocker_for_task(states, task_id)
    if st is None or not st.is_open or not task_applies(conn, st, task_id):
        return None
    others = [t for t in st.behind if t != task_id and task_applies(conn, st, t)]
    if others:
        marks = ",".join("?" * len(others))
        if conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({marks}) AND status = 'running' LIMIT 1", others,
        ).fetchone():
            return GUARD_REASON   # a probe is already in flight
    if _released_as_probe(conn, st, task_id):
        return None
    if interval is None:
        interval = probe_interval_seconds()
    last = max(st.probe_starts.values(), default=min(st.behind.values()))
    if now - last >= interval:
        return None
    # A human unblock after the newest assertion is a deliberate "try again now".
    if _unblocked_since_assertion(conn, task_id, st.blocker_id):
        return None
    return GUARD_REASON


def _released_as_probe(conn: sqlite3.Connection, st: BlockerState, task_id: str) -> bool:
    """True when ``task_id`` was named the probe for ``st`` (a :data:`PROBE_RELEASED_EVENT` for it) at or after
    the run that put it behind ``st``, and has not started a run since: the release is spent by one run. The CI
    sweep (``kanban_ci_progress.apply``) names the first card it wakes for a runner blocker this way, since that
    card discovered the wall and holding it ``shared_blocker_probe_seconds`` would only delay the fix.

    The newest release for ``st`` decides. One that names its asserting run (``after_run``) is spent only by a
    later run of the card: ``probe_starts`` also holds runs from before a re-assertion (a card the sweep wakes a
    second time for the same blocker ran in between, and the wake's own synthesized run is recorded too), so it
    cannot tell a fresh release from a spent one. A release without ``after_run`` (:func:`probe_stranded_blockers`,
    older rows) falls back to ``probe_starts`` and also needs no re-assertion of ``st`` after it.

    Before PR #2040 the guard ignored releases: a card :func:`probe_stranded_blockers` unblocked spawned only through
    the probe interval below, so another card re-asserting ``st`` meanwhile (one that hit the wall after the
    release) held it for another interval. Honouring a stranded release regardless would let it spawn right
    behind that fresh evidence, a second probe in one interval; the re-assertion check keeps main's behaviour
    there. With no re-assertion since, the release differs from the interval rule only when another card's probe
    run started after it and ended without settling the wall (a crash): the released card may then run at once.
    [daveey]"""
    from hermes_cli import kanban_db as kb

    asserted = st.behind[task_id]  # the guard only asks about a card behind st
    for row in conn.execute(
        "SELECT created_at, payload FROM task_events WHERE task_id = ? AND kind = ? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 20", (task_id, PROBE_RELEASED_EVENT, asserted),
    ):
        payload = kb._json_dict(row["payload"])
        if normalize_blocker_id(payload.get("blocker_id")) != st.blocker_id:
            continue
        after = payload.get("after_run")
        if type(after) is int:
            return conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND id > ? LIMIT 1",
                                (task_id, after)).fetchone() is None
        released = kb._to_epoch(row["created_at"]) or 0
        return task_id not in st.probe_starts and st.asserted_at <= released
    return False


def release_cleared_blockers(conn: sqlite3.Connection, *, now: Optional[int] = None,
                             oldest_out: Optional[list] = None) -> list[str]:
    """Wake parked cards and retire ready/review hold evidence after a clear.
    Returns only task ids whose lane changed (ready/review cards remain queued)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    # Retained executor copies own host-local wake probes, not shared-wall policy writes.
    foreign = kwc.foreign_ids(conn)
    released: list[str] = []
    for st in blocker_states(conn, now=now, oldest_out=oldest_out).values():
        if st.is_open or st.cleared_by is None:
            continue
        by_task, by_run = st.cleared_by
        for tid, asserted in sorted(st.behind.items()):
            if tid in foreign or asserted > st.cleared_at or tid == by_task:
                continue
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None or row["status"] not in ("blocked", "scheduled", "ready", "review"):
                continue
            if _already_saw_clear(conn, kb, tid, st):
                continue
            payload = {"blocker_id": st.blocker_id, "cleared_by_task": by_task, "cleared_by_run": by_run,
                       "cleared_via": st.cleared_via or "run"}
            if row["status"] in ("ready", "review"):
                # Admission may be capacity-limited before the guard is evaluated again.
                # Retire the historical hold without changing lanes or bypassing admission.
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, CLEARED_EVENT, payload)
                continue
            if row["status"] == "blocked":
                if not kb.unblock_task(conn, tid):
                    continue
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, CLEARED_EVENT, payload)
            else:
                with kb.write_txn(conn):
                    if kwc._wake(conn, tid, CLEARED_EVENT, payload) is None:
                        continue
            how = ("registered shared-blocker check exited 0" if st.cleared_via == "check" else
                   f"{by_task}'s wake_check exited 0" if st.cleared_via == CONDITION_MET_EVENT
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
                            interval: Optional[int] = None, due_out: Optional[list] = None) -> list[str]:
    """Unblock ONE ``blocked`` card per open blocker that nothing else would probe. Returns task ids.

    :func:`shared_blocker_guard` only spaces out ``ready`` cards, so a blocker whose cards are all
    ``blocked`` (a lone card, or every card re-asserted) was never probed and never cleared: the
    cards sat forever (t_e7dc822c). When no card behind the blocker is ``ready`` or ``running`` and
    the probe interval has passed since the newest assertion or probe start, the card whose
    assertion is oldest is unblocked; it then spawns as the probe. Cards carrying a human ask
    (``human_request``) are left for the human. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    foreign = kwc.foreign_ids(conn)
    now = int(time.time()) if now is None else int(now)
    if interval is None:
        interval = probe_interval_seconds()
    probed: list[str] = []
    for st in blocker_states(conn, now=now, oldest_out=due_out).values():
        if not st.is_open or not st.behind:
            continue
        if st.blocker_provider and due_out is not None:
            due_out.append(now + ROUTE_RECHECK_SECONDS)
        ids = sorted(t for t in st.behind if task_applies(conn, st, t))
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, status, human_request FROM tasks WHERE id IN ({marks})", ids).fetchall()
        if any(r["status"] in ("ready", "running") for r in rows):
            continue   # the ready-card guard already spaces those probes
        last = max(st.probe_starts.values(), default=min(st.behind.values()))
        if now - last < interval:
            if due_out is not None:
                due_out.append(last + interval)  # G2: the time policy's next due moment for this blocker
            continue
        candidates = sorted((st.behind[r["id"]], r["id"]) for r in rows
                            if r["status"] == "blocked" and not r["human_request"])
        for _asserted, tid in candidates:
            # Elect across all visible cards, then defer to the winner's home. Filtering
            # foreign candidates first would elect a different probe on each gateway.
            if tid in foreign:
                break
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
    from hermes_cli.kanban_shared_blocker_checks import registered_checks
    checks = registered_checks()
    lines = ["## Open shared blockers on this board",
             "External walls other cards already hit. Stop early only when the step this run is about "
             "to take needs the walled capability; then end "
             "with kanban_block/kanban_continue passing the same `blocker_id` instead of rediscovering "
             "it; only one card probes a blocker at a time. A timed wait (kind='time' or a "
             "SCHEDULED_UNTIL= reason) keeps its wake time and wake_check when it also passes "
             "blocker_id."]
    if any(st.blocker_id in checks for st in states):
        lines[1] = ("For walls with a registered check, never re-assert from this context alone: "
                    "run the live check when your next step needs the capability. For walls without a "
                    "registered check, stop at the affected step and end with the same blocker_id; "
                    "only one card probes those walls at a time.")
    model, provider, lane = task_route(conn, task_id)
    lines.append(f"Current route: provider={provider or 'unknown'}, model={model or 'unknown'}, lane={lane}.")
    lines.append("Provider-scoped walls apply only to that provider. Continue unrelated work; if a later "
                 "step needs the affected provider, wait at that step, not before it. Model/lane on a "
                 "blocker describe where it was observed, not additional restrictions.")
    lines.append("Reasserting a listed id whose provider is not 'all': pass the same `blocker_provider` "
                 "with the `blocker_id` (e.g. `blocker_provider: \"anthropic\"`). A reassertion without it, "
                 "or with another provider, makes the id board-wide and holds every provider's cards.")
    mine = None
    for st in sorted(states, key=lambda s: -s.asserted_at):
        applicable = _applies(st, provider)
        if task_id in st.behind and applicable:
            mine = st
        scope = (f"provider={st.blocker_provider or 'all'}, model={st.blocker_model or 'unspecified'}, "
                 f"lane={st.blocker_lane or 'unspecified'}; "
                 f"{'applicable' if applicable else 'not applicable'} to current route")
        lines.append(f"- `{st.blocker_id}` ({len(st.behind)} card(s) parked): {st.reason or '(no reason)'} [{scope}]")
        if st.blocker_id in checks:
            lines.append("  The dispatcher re-checks this wall every 2 min; if your next step needs it, "
                         f"run `{checks[st.blocker_id]}`; exit 0 = continue, else end with "
                         f"`blocker_id: \"{st.blocker_id}\"`. Never re-assert without a live check.")
    if mine is not None:
        again = f"`blocker_id: \"{mine.blocker_id}\"`"
        if mine.blocker_provider:
            again += f" and `blocker_provider: \"{mine.blocker_provider}\"`"
        lines.append("")
        lines.append(
            f"This run is the probe for `{mine.blocker_id}`. Check first whether it still holds. If it "
            f"does, end again with {again}. If it is gone, carry on: "
            "completing or handing off clears it and wakes the other parked cards. A park without the "
            "id does NOT clear it; if you park for another reason and saw the wall gone, pass "
            "`clear_blocker: true`.")
    lines.append("")
    return "\n".join(lines)
