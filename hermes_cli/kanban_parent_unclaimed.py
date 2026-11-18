"""Tell a waiting chat when the parent its card waits on sits ready and unclaimed. [daveey]

Defect kanban-447c98343f (2026-11-02): a rollout card parked at 19:48 on its parent, which sat ready from 19:04 to
21:04 without a worker; the chat that owned the rollout heard nothing and David asked "what are we waiting for"
three times. The dispatcher now runs a card others wait on first (``DISPATCH_ORDER``), but a busy or capped host can
still leave it queued; this module makes that visible to the chat instead of to David.

:func:`sweep` (run by every non-dry dispatcher tick) appends ONE ``dependency_parent_unclaimed`` event per wait to
a chat-owned ``todo`` child (it has a notify subscription or a ``session_id``) whose latest ``dependency_wait`` is
at least :data:`THRESHOLD_SECONDS` old while a parent has been ready and unclaimed that long. The gateway and TUI
notifiers deliver the event to the child's chats with :func:`notice_text`. :func:`ready_hint` is the matching
``kanban_show`` line for the parent itself.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from hermes_cli import kanban_last_checkpoint as last_checkpoint

_log = logging.getLogger(__name__)

NOTICE_KIND = "dependency_parent_unclaimed"
THRESHOLD_SECONDS = 30 * 60
NO_FIX = "a card that can't start is no fix"


def _minutes(seconds: float) -> int:
    return max(0, int(seconds // 60))


def ready_since(conn, task_id: str, created_at: Optional[int] = None) -> int:
    """When ``task_id`` last entered ``ready`` (the stranded-in-ready diagnostic's floor, its latest run's end
    included), else its creation."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_diagnostics import _last_ready_entry_ts
    runs = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()
    since = _last_ready_entry_ts(kb.list_events(conn, task_id), [dict(r) for r in runs])
    return int(since or created_at or 0)


def _place(conn, parent_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_queue_position as kqp
    try:
        return kqp.position(conn, kb.get_task(conn, parent_id)) or {}
    except Exception:  # health: allow BLE001 -- a queue place is advisory; the notice still goes out
        _log.debug("parent-unclaimed: queue position unavailable for %s", parent_id, exc_info=True)
        return {}


def _queue_words(place: dict[str, Any], now: Optional[float] = None) -> str:
    from hermes_cli.kanban_queue_position import reroute_prefix
    if place.get("lane") == "unplaceable":
        return _lane_words(place, now)  # ``describe`` already names the reroute
    return reroute_prefix(place) + _lane_words(place, now)


def _lane_words(place: dict[str, Any], now: Optional[float] = None) -> str:
    if place.get("lane") == "worker_pool":
        return f"waiting for a pool worker ({place.get('reason') or 'no reason recorded'})"
    if place.get("lane") in ("machine", "unplaceable"):
        # A card that runs on another machine has no local queue place; "queue place unknown" hid a drain or a
        # hold on that machine from the chat (t_01a1cca8ed755919). [daveey]
        from hermes_cli import kanban_queue_position as kqp
        return kqp.headline(place) if place["lane"] == "machine" else str(kqp.describe(place))
    from hermes_cli.kanban_queue_position import ahead_words
    from hermes_cli import kanban_dispatch_skip as skips
    ahead = place.get("ahead")
    words = ahead_words(place, "{n} card(s) ahead of it") if ahead is not None else "queue place unknown"
    if place.get("running") is not None and place.get("limit") is not None:
        words += f", {place['running']}/{place['limit']} worker slots busy"
    elif place.get("limit") is not None and "running" in place:
        words += f", running count unknown (cap {place['limit']})"
    if place.get("skip"):  # the recorded reason comes first: a bare queue place read as an ordering wait [daveey]
        words = f"{skips.words(place['skip'], now=now)}; {words}"
    return words


def _pool_sentence(place: dict[str, Any], now: Optional[float] = None) -> str:
    """The pool re-place sentence when a pool worker could start the card now, else ``""``. Kept out of
    :func:`_queue_words`: the notices print that clause in parentheses, and their advice already says not to run the
    work outside the dispatcher (PR #1821 review). Dropped from these notices before (kanban-e7a33105a4). [daveey]"""
    from hermes_cli.kanban_dispatch_skip import pool_replace_helps
    if not place.get("pool_could_run") or not pool_replace_helps(place.get("skip"), now=now):  # quota wall (#1835)
        return ""
    from hermes_cli.kanban_queue_position import pool_words
    return pool_words(place, advice=False)


def _backlog(place: dict[str, Any]) -> dict[str, Any]:
    """``{"label_backlog": ...}`` for a machine-lane place whose matching machines are busy with same-label cards
    (``kanban_label_backlog``), else ``{}``."""
    from hermes_cli import kanban_label_backlog as klb
    return {"label_backlog": place["backlog"]} if klb.words(place.get("backlog")) else {}


def _pool_suffix(payload: dict[str, Any]) -> str:
    """``" <pool hint>."`` for a notice payload that carries one, else ``""``."""
    hint = str(payload.get("pool_hint") or "").strip()
    return f" {hint}." if hint else ""


def _observation(payload: dict[str, Any]) -> str:
    """``board <b>, observed <HH:MM:SSZ>, admission reason <r | unknown>`` for the notice text (empty on an old
    payload without those fields). A pacing admission names the hold and its until time after the reason."""
    if "observed_at" not in payload:
        return ""
    at = int(payload["observed_at"])
    admission = payload.get("admission") if isinstance(payload.get("admission"), dict) else {}
    reason = admission.get("reason") or "unknown"
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    held = pacing.admission_skip(admission)
    if held:
        from hermes_cli.kanban_dispatch_skip import words
        reason = f"{reason}: {words(held, now=at)}"
    return (f" [board {payload.get('board') or 'unknown'}, observed {time.strftime('%H:%M:%SZ', time.gmtime(at))}, "
            f"admission reason {reason}]")


def _options(parent_id: str, evidence: Optional[dict[str, Any]] = None,
             payload: Optional[dict[str, Any]] = None, now: Optional[float] = None) -> str:
    """Capacity advice, or the recorded skip's (or the unrecorded skip's) advice without priority changes. A card
    whose matching machines claimed same-label cards during the wait is queued, so no re-place is offered; one whose
    pool the LLM pacing governor holds starts on its own, so neither is a priority change or a re-place. A stored
    pacing admission is checked first: a notice records one over same-label claims only for a held pool
    (:func:`_queued`)."""
    from hermes_cli import kanban_label_backlog as klb
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    from hermes_cli.kanban_dispatch_skip import advice as skip_advice
    held = pacing.admission_skip((payload or {}).get("admission"))
    if held:
        return f"{NO_FIX}: " + skip_advice(held, now=now)
    if klb.busy((payload or {}).get("label_backlog")):
        return f"Not a placement problem, do not re-place: {klb.ADVICE}"
    from hermes_cli.kanban_dispatch_skip import notice_advice
    from hermes_cli.kanban_priority import advice
    return f"{NO_FIX}: " + (notice_advice(payload or {}, now=now) or advice(parent_id, evidence))


def notice_text(payload: Any) -> str:
    """Chat line for the event (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    parent = str(payload.get("parent") or "<parent>")[:64]
    title = str(payload.get("parent_title") or "").strip()[:120]
    named = f"{parent} ({title})" if title else parent
    advice = ("Check the current dispatcher verdict and resolve the recorded guard; priority changes do not clear it"
              if payload.get("respawn_guard") else _options(parent, payload.get("priority_evidence"), payload))
    return (f"Waiting {payload.get('wait_minutes', '?')} min on parent {named}, which has been ready "
            f"{payload.get('ready_minutes', '?')} min with no worker ({payload.get('queue') or 'queue place unknown'})"
            + _observation(payload) + "." + _pool_suffix(payload) + " " + last_checkpoint.sentence(payload) + advice
            + ".")


def ready_hint(conn, task, *, now: Optional[float] = None) -> Optional[str]:
    """``kanban_show`` hint for a card ready and unclaimed at least :data:`THRESHOLD_SECONDS`; None otherwise."""
    if task is None or task.status != "ready" or getattr(task, "claim_lock", None):
        return None
    now = time.time() if now is None else now
    waited = now - ready_since(conn, task.id, task.created_at)
    if waited < THRESHOLD_SECONDS:
        return None
    from hermes_cli.kanban_priority import lane_evidence
    from hermes_cli import kanban_queue_position as kqp
    place = _place(conn, task.id)
    checkpoint = last_checkpoint.sentence(last_checkpoint.fields(conn, task.id))  # kanban-3debc730ab [daveey]
    if place.get("lane") == "respawn_guard":
        return f"Ready {_minutes(waited)} min, unclaimed; {kqp.describe(place)}. {checkpoint}".rstrip()
    from hermes_cli.kanban_dispatch_skip import notice_fields
    pool = _pool_sentence(place, now)
    fields = {**notice_fields(place, waited), **({"pool_hint": pool} if pool else {}), **_backlog(place)}
    from hermes_cli import kanban_host_counts as khc
    from hermes_cli import kanban_label_backlog as klb
    held_over_claims = _held_over_claims(conn, task.id, place)
    admission = held_over_claims or (None if klb.busy(place.get("backlog"))
                                     else _pacing_admission(conn, task.id, place, None))
    if admission:
        fields.update(admission=admission, board=place.get("board") or khc.board_of(conn),
                      observed_at=place.get("observed_at") or int(now))
    return (f"Ready {_minutes(waited)} min, unclaimed; {_queue_words(place, now)}" + _observation(fields) + ". "
            + (f"{pool}. " if pool else "")
            + checkpoint + _options(task.id, lane_evidence(conn, task.id), fields, now) + ".")


_CANDIDATES = (
    "SELECT c.id AS child, p.id AS parent, p.title AS parent_title, p.created_at AS parent_created "
    "FROM tasks c JOIN task_links l ON l.child_id = c.id JOIN tasks p ON p.id = l.parent_id "
    "WHERE c.status = 'todo' AND p.status = 'ready' AND p.claim_lock IS NULL "
    "AND (c.session_id IS NOT NULL OR EXISTS (SELECT 1 FROM kanban_notify_subs s WHERE s.task_id = c.id)) "
    "ORDER BY c.id, p.id"
)


def sweep(conn, *, now: Optional[float] = None) -> int:
    """Append the once-per-wait notice for every qualifying (child, parent); returns how many were appended."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_replica_freshness import local_copy_stale
    now = time.time() if now is None else now
    posted = 0
    rows = conn.execute(_CANDIDATES).fetchall()
    if rows and local_copy_stale(conn, "parent-unclaimed notice"):  # a stale copy's "unclaimed" proves nothing
        return 0
    for row in rows:
        wait = conn.execute(
            "SELECT id, created_at FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' "
            "ORDER BY id DESC LIMIT 1", (row["child"],)).fetchone()
        if wait is None:
            continue
        waited = now - (kb._to_epoch(wait["created_at"]) or now)
        ready_for = now - ready_since(conn, row["parent"], row["parent_created"])
        if waited < THRESHOLD_SECONDS or ready_for < THRESHOLD_SECONDS:
            continue
        if post_notice(conn, row["child"], row["parent"], wait_key=wait["id"], waited_seconds=waited,
                       ready_seconds=ready_for, parent_title=row["parent_title"]):
            posted += 1
    return posted


def safe_sweep(conn) -> int:
    """:func:`sweep` for the dispatcher tick, then the direct chat-wait sweep (``kanban_chat_wait``, same
    cadence; kept here so the dispatcher facade does not grow). A notice failure is logged and never stops
    dispatch."""
    from hermes_cli import kanban_chat_wait
    try:
        posted = sweep(conn)
    except Exception:  # health: allow BLE001 -- a notice failure must never stop dispatch
        _log.warning("kanban dispatcher: parent-unclaimed notice sweep failed", exc_info=True)
        posted = 0
    from hermes_cli import kanban_pickup_stall
    kanban_pickup_stall.safe_sweep(conn)  # pinned foreign cards this worker node has not picked up [daveey]
    from hermes_cli import worker_resources_unknown_alarm
    worker_resources_unknown_alarm.safe_sweep(conn)  # every claim here refused worker_resources_unknown
    from hermes_cli import turn_urgency_status_alarm
    turn_urgency_status_alarm.safe_sweep(conn)
    return posted + kanban_chat_wait.safe_sweep(conn)


def _already_posted(conn, task_id: str, waiting_on: str, wait_key: Any, kind: str) -> bool:
    from hermes_cli import kanban_db as kb
    return any(kb._json_dict(p).get("wait_event_id") == wait_key and kb._json_dict(p).get("parent") == waiting_on
               for (p,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                        (task_id, kind)))


def _read_pacing(conn, task_id):
    """Keep advisory governor access at the notice's audited read-only boundary."""
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    paces = pacing.paced_pools(conn, task_id)
    return pacing.admission(paces) if paces else None


def _pacing_admission(conn, task_id: str, place: dict[str, Any], extra: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The ``admission`` for a card that only other machines can lease while this gateway's own LLM pacing governor
    holds every provider it may run on (t_01a1db9d9538716b); None otherwise, so the notice keeps its words.

    Workers import other gateways' cards in priority order, so a low-ranked card gets no verdict from any dispatcher
    able to lease it, and its wait is the pool's pace (``kanban_llm_pacing_dispatch.paced_pools``: pool workers draw
    the same accounts from this gateway's relay). A hold on every matching machine, a card the Board does not hold, a
    verdict from a dispatcher able to lease the card and a read that fails or is not positive all leave it alone."""
    if place.get("lane") != "machine" or (extra or {}).get("missing_shared_board"):
        return None
    if set(place.get("machines") or ()) <= {*(place.get("held") or ()), *(place.get("draining") or ())}:
        return None  # every machine that could lease it is on hold or draining: that wait ends differently
    try:
        from hermes_cli import kanban_dispatch_skip as skips
        if not all(skips.recorder_cannot_lease(skip) for skip in skips.episode_skips(conn, task_id)):
            return None  # a dispatcher able to lease it recorded a verdict: the notice does not talk over it
        return _read_pacing(conn, task_id)
    except Exception:  # health: allow BLE001 -- the governor read is advisory; the notice still goes out as before
        _log.debug("parent-unclaimed: pacing read unavailable for %s", task_id, exc_info=True)
        return None


def _held_over_claims(conn, task_id: str, place: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The pacing ``admission`` for a card whose place shows same-label claims (``kanban_label_backlog.busy``) while
    the governor holds EVERY pool the card may run on; None otherwise.

    Claims prove a queue only for a pool that admits starts. A ``held`` pool admits none on any worker, so claims of
    same-label cards routed to other providers say nothing about this card (agent-b460ba9d37: openai-codex held for
    five days, yet a default-routed card's notice said ``matching_pool_busy`` because anthropic cards were claimed,
    and its owner chat forecast pickup as slots freed). A ``paced`` pool still admits starts, so its claims stand.
    The governor read is advisory: a failing or non-positive read keeps the busy verdict. [daveey]"""
    from hermes_cli import kanban_label_backlog as klb
    if not klb.busy(place.get("backlog")):
        return None
    try:
        paces = _read_pacing(conn, task_id)
    except Exception:  # health: allow BLE001 -- the governor read is advisory; keep the queue verdict
        _log.debug("parent-unclaimed: pacing read unavailable for %s", task_id, exc_info=True)
        return None
    pools = (paces or {}).get("providers") or []
    return paces if pools and all(p.get("state") == "held" for p in pools) else None  # lp.HELD, by value (NOTICE_VIEW)


def _queued(conn, task_id: str, place: dict[str, Any]) -> bool:
    """True when same-label claims make the wait a queue wait: busy, and no held pool (:func:`_held_over_claims`)."""
    from hermes_cli import kanban_label_backlog as klb
    return klb.busy(place.get("backlog")) and _held_over_claims(conn, task_id, place) is None


def _host_hold_only_if_local(extra: Optional[dict[str, Any]], place: dict[str, Any]) -> Optional[dict[str, Any]]:
    """``extra`` without this host's core-update hold (``core_update_*``) unless this host leases the card (lane
    ``local``). The hold only stops this host's claims: a pool or remote-placed card is leased elsewhere, so its
    notice keeps the queue/pacing reason (41 pool notices blamed the hub's own update swap, t_01a1e01b9645d70c).
    An unknown place is not proof the host leases it, so it drops the hold too. [daveey]"""
    if not extra or not extra.get("core_update_hold") or place.get("lane") == "local":
        return extra
    return {k: v for k, v in extra.items() if not k.startswith("core_update_")}


def post_notice(conn, task_id: str, waiting_on: str, *, wait_key: Any, waited_seconds: float,
                ready_seconds: float, parent_title: Optional[str] = None, kind: str = NOTICE_KIND,
                extra: Optional[dict[str, Any]] = None) -> bool:
    """Append ONE ``kind`` event (default ``dependency_parent_unclaimed``) to ``task_id`` (its chats receive it
    through their notify subscriptions) about ``waiting_on``, a card still ready and unclaimed. ``wait_key``
    identifies the wait: a second call with the same (waiting_on, wait_key) posts nothing. Reused for a direct chat
    wait (``kanban_chat_wait``), where ``waiting_on`` is ``task_id`` itself; ``extra`` adds payload fields (its
    clock hold). Returns True when it posted."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_host_counts as khc
    from hermes_cli import kanban_queue_position as kqp
    from hermes_cli import kanban_label_backlog as klb
    if _already_posted(conn, task_id, waiting_on, wait_key, kind):
        return False  # before the queue place: every tick asks again for each long-ready card [daveey]
    place = _place(conn, waiting_on)
    extra = _host_hold_only_if_local(extra, place)
    busy = _queued(conn, waiting_on, place)
    from hermes_cli import kanban_notice_verdict
    verdict = ({} if busy or (extra or {}).get("core_update_hold") else
               kanban_notice_verdict.fields(conn, waiting_on, place, ready_seconds, _read_pacing))
    observed = {"board": place.get("board") or khc.board_of(conn), "observed_at": place.get("observed_at") or int(time.time()),
                "admission": ({"reason": "core_update_hold", "status": "held"} if (extra or {}).get("core_update_hold")
                              else {"reason": klb.REASON, "status": "queued"} if busy
                              else verdict.get("admission") or _pacing_admission(conn, waiting_on, place, extra)
                              or {"reason": None, "status": "unknown"})}
    guard = place.get("reason") if place.get("lane") == "respawn_guard" else None
    pool_hint = "" if guard else _pool_sentence(place, observed["observed_at"])  # outside the queue clause (#1821)
    checkpoint = last_checkpoint.notice_fields(conn, waiting_on)  # same lookup supplies fields and evidence [daveey]
    with kb.write_txn(conn):
        if _already_posted(conn, task_id, waiting_on, wait_key, kind):
            return False
        still = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (waiting_on,)).fetchone()
        if still is None or still["status"] != "ready" or still["claim_lock"]:
            return False
        from hermes_cli.kanban_dispatch_skip import notice_fields
        from hermes_cli.kanban_priority import lane_evidence
        kb._append_event(conn, task_id, kind, {
            "parent": waiting_on, "parent_title": parent_title, "wait_event_id": wait_key,
            "wait_minutes": _minutes(waited_seconds), "ready_minutes": _minutes(ready_seconds),
            "queue": kqp.describe(place) if guard else _queue_words(place, observed["observed_at"]),
            **observed, **checkpoint,
            **({"pool_hint": pool_hint} if pool_hint else {}),
            **(extra or {}),
            **_backlog(place),
            "respawn_guard": guard,
            "priority_evidence": lane_evidence(conn, waiting_on),
            **({} if guard else {**notice_fields(place, ready_seconds), **verdict}),
        })
    return True
