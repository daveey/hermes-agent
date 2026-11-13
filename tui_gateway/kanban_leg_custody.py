"""Which leg holds a chat's tui Kanban notices: the canonical gateway, the chat's waking ``local`` row, or this
legacy Desktop backend (t_01a1cfbe6c8d46e7). [daveey]

Chat 20261026_235722_bef6c6 on the MacBook, 2026-11-12: t_01a1cfb6db097701's completion reached the chat twice
(the local wake at 04:40:25, the tui claim the busy backend had buffered at 04:59:20), and t_01a1ce412fbf6e7f's
waited 45 min, held in flight by a backend whose turns the gateway-owned chat refused.
"""
import contextlib
import logging
import os

logger = logging.getLogger(__name__)


def gateway_owns(session: dict, ledger_refusal, lineage: tuple = (), lease_holder=None) -> bool:
    """The canonical gateway runs this chat now: its durable ledger holds work for it that ends on its own
    (``owner_forward.FORWARDABLE_LEDGER_STATES``), or the live gateway process holds the chat's turn lease. Those
    are the conditions under which ``gateway.kanban_tui_owner`` claims the chat's tui rows and admits them to its
    FIFO within one notifier tick (``_gateway_holds_work``), checked over the same ids: the chat and its compaction
    parents (``lineage``; admissions and leases are keyed by the lineage root, tui rows by the tip — review
    t_01a1d1543b9d13cf). ``ledger_refusal`` is ``session_lifecycle._canonical_ledger_refusal``; ``lease_holder``
    maps a session id to its turn-lease holder string (``pid=<pid>:…``) or None. An ``unknown`` admission, no
    ledger row, a lease held by this or any non-gateway process, or an unreadable answer: False, and this backend
    keeps the rows as before (a legacy-only chat buffers while busy)."""
    from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES
    key = str(session.get("session_key") or "")
    ids = tuple(dict.fromkeys(s for s in (key, *lineage) if s))
    for sid in ids:
        try:
            refusal = ledger_refusal(session if sid == key else {**session, "session_key": sid})
        except Exception:  # health: allow BLE001 -- an unprovable owner keeps the legacy path
            return False
        if refusal is not None:
            return getattr(refusal, "ledger_status", None) in FORWARDABLE_LEDGER_STATES
    if lease_holder is None:
        return False
    try:
        mine = f"pid={os.getpid()}:"
        others = [h for h in (str(lease_holder(sid) or "") for sid in ids) if h.startswith("pid=")
                  and not h.startswith(mine)]
        if not others:  # the common case: no lease, or this backend's own turn (no gateway probe)
            return False
        gateway = _live_gateway_pid()
        return gateway is not None and any(h.startswith(f"pid={gateway}:") for h in others)
    except Exception:  # health: allow BLE001 -- an unprovable owner keeps the legacy path
        return False


def _lineage(session: dict, session_lineage) -> tuple:
    try:
        return tuple(session_lineage(str(session.get("session_key") or "")))
    except Exception:  # health: allow BLE001 -- no lineage: the chat's own id still answers
        return ()


def legacy_gateway_owns(session: dict, ledger_refusal, session_lineage, session_db) -> bool:
    """:func:`gateway_owns` for a legacy backend's session: its compaction parents (``session_lineage``) and the
    turn-lease holder read through the session's own store (``session_db``)."""
    def lease_holder(sid: str):
        with session_db(session) as db:
            owner_of = getattr(db, "session_turn_lease_owner", None)
            owner = owner_of(sid) if owner_of is not None else None
        return (owner or {}).get("holder")
    return gateway_owns(session, ledger_refusal, _lineage(session, session_lineage), lease_holder)


def legacy_settle_held(session: dict, session_lineage, log_failure) -> None:
    settle_held(session, _lineage(session, session_lineage), log_failure)


def _live_gateway_pid():
    from hermes_cli.gateway_multiplex_served import live_default_gateway_pid
    return live_default_gateway_pid()


def _settled(row, old: int) -> int:
    """The event id up to which this ``tui`` row no longer owes anything to an outstanding claim.

    With a claim in flight that is ``inflight_from`` (the wake's settle raises it past what it carried). With none,
    it is the cursor: recovery clears a claim the wake settled through its end (``(end, end]``, nothing to rewind)
    and keeps the cursor there, and a rewound claim puts the cursor back at its start. Falling back to the buffer's
    own ``old`` there kept a completion the wake had already delivered once the archived local row drained (review
    t_01a1d154902ba0a7). No row: the claim's own start (the tui leg still owes it)."""
    if row is None:
        return old
    return int(row[0]) if row[0] is not None else int(row[1] or 0)


def settle_held(session: dict, lineage: tuple, log_failure) -> None:
    """Resolve the buffered claims whose events the chat's waking ``local`` row delivered meanwhile.

    A claim taken while the chat was busy waits in the session buffer; the wake can deliver its events first. It
    then settles the tui row (``inflight_from`` moves past them, ``kanban_wake_sibling.settle_tui_sibling``) or its
    own cursor passes them (``tui_cover``). Such a text is dropped; a claim left with nothing to deliver is acked,
    which advances the cursor and clears the claim with no failure counted. Running it anyway gave
    t_01a1cfb6db097701 a second turn (rows 879522 and 880235), and handing it back let a later consumer re-claim
    it. ``lineage`` = the chat's compaction parents (its local rows may sit on the root)."""
    claims = session.get("_kanban_claims") or []
    if not claims:
        return
    from gateway.kanban_wake_sibling import tui_cover
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    dropped: set = set()
    for claim in list(claims):
        texts = claim.get("texts") or []
        if claim.get("durable") or claim.get("remote") or claim.get("acked") or not texts:
            continue
        ident = dict(task_id=claim["task_id"], platform=claim["platform"], chat_id=claim["chat_id"],
                     thread_id=claim.get("thread_id") or "")
        try:
            with contextlib.closing(_kbc.connect(board=claim["board"])) as conn:
                row = conn.execute("SELECT inflight_from, last_event_id FROM kanban_notify_subs WHERE task_id = ? "
                                   "AND platform = ? AND chat_id = ? AND thread_id = ?",
                                   tuple(ident.values())).fetchone()
                settled = _settled(row, int(claim["old"]))
                cover = tui_cover(conn, {**ident, "sibling_chats": lineage})
                done = {id(t) for t in texts if (ev := getattr(t, "_kb_event", None)) is not None and (
                    int(getattr(ev, "id", 0) or 0) <= settled or (cover is not None and cover(ev)))}
                if not done:
                    continue
                dropped |= done
                claim["texts"] = [t for t in texts if id(t) not in done]
                if claim["texts"]:
                    continue
                if _kbn.ack_notify_claim(conn, upto=claim["new"], from_cursor=claim["old"], **ident):
                    claims.remove(claim)
                elif not _kbn.notify_claim_in_flight(conn, **ident):
                    claims.remove(claim)  # recovery already cleared the settled range: nothing left to ack
                else:
                    claim["durable"] = True  # an earlier claim is still pending: the next ack run covers it
        except Exception as exc:  # health: allow BLE001 -- unreadable: the batch delivers as before
            log_failure("kanban held-claim custody check failed", exc)
    if dropped:
        logger.info("kanban notices: %d buffered notice(s) for %s were delivered by the chat's wake; resolved",
                    len(dropped), session.get("session_key"))
        with session["history_lock"]:
            session["_kanban_pending"] = [t for t in session.get("_kanban_pending") or [] if id(t) not in dropped]
