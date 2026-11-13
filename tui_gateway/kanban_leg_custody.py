"""Which leg holds a chat's tui Kanban notices: the canonical gateway, the chat's waking ``local`` row, or this
legacy Desktop backend (t_01a1cfbe6c8d46e7). [daveey]

Chat 20261026_235722_bef6c6 on the MacBook, 2026-11-12: t_01a1cfb6db097701's completion reached the chat twice
(the local wake at 04:40:25, the tui claim the busy backend had buffered at 04:59:20), and t_01a1ce412fbf6e7f's
waited 45 min, held in flight by a backend whose turns the gateway-owned chat refused.
"""
import contextlib
import logging
import os
import time
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

# How long one chat's gateway-owner answer is reused: no longer than the legacy poll (``_KANBAN_POLL_SECONDS``),
# so a poll sees an answer at most one poll old. Each answer opens state.db once; every live chat's poller asked
# for it up to twice per 5 s poll, once per lineage id (review 5887848770 nit 2, the cost t_01a1cee98b798343 took
# out of ``_session_lineage``). No hook tells this process when the gateway's ledger or lease changes.
OWNER_TTL_S = 5.0

_owners: dict = {}  # session key -> the GatewayOwner its last answer named (the per-row check in the board poll)


class GatewayOwner(NamedTuple):
    """The live gateway that will claim this chat's tui rows: its pid, its launch profile, and why."""
    pid: int
    profile: str
    reason: str


def gateway_owner(ids: tuple, ledger_status: Optional[str], lease_holders: tuple, gateway_pid=None,
                  launch_profile=None) -> Optional[GatewayOwner]:
    """The live gateway whose collector claims this chat's tui rows and admits them to its FIFO within one
    notifier tick (``gateway.kanban_tui_owner.owned_live_session``), else None.

    The chat-level half of that predicate, as this process can see it: the verified live default gateway
    (``gateway_pid``) holds work for the chat that ends on its own, i.e. the first ledger row over ``ids`` (the
    chat and its compaction parents: admissions and leases sit on the lineage root, tui rows on the tip, review
    t_01a1d1543b9d13cf) is in ``owner_forward.FORWARDABLE_LEDGER_STATES``, or its process holds the chat's turn
    lease (``_gateway_holds_work``). The row-level half is :func:`claims_row`. No live gateway (its LOCAL
    adapter is what claims), an ``unknown`` admission, no ledger row, a lease held by any other process, or an
    unreadable answer: None, and this backend keeps the rows as before (a legacy-only chat buffers while busy)."""
    from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES
    if ledger_status is not None:
        reason = f"ledger {ledger_status}" if ledger_status in FORWARDABLE_LEDGER_STATES else None
    else:
        mine = f"pid={os.getpid()}:"
        others = [h for h in lease_holders if h.startswith("pid=") and not h.startswith(mine)]
        reason = "turn lease" if others else None  # the common case: no lease (no gateway probe)
    if reason is None:
        return None
    pid = _live_gateway_pid() if gateway_pid is None else gateway_pid
    if pid is None or (reason == "turn lease" and not any(h.startswith(f"pid={pid}:") for h in lease_holders)):
        return None
    profile = _gateway_launch_profile(pid) if launch_profile is None else launch_profile
    return GatewayOwner(int(pid), str(profile or ""), reason)


def claims_row(owner: Optional[GatewayOwner], sub: dict) -> bool:
    """Whether ``owner``'s collector claims this tui row: ``_launch_authority`` serves only the gateway's launch
    profile (an unstamped row goes to it), and ``owned_live_session`` skips threaded rows. A row it would skip
    stays with this backend, which delivers it once the chat is back here (review 5887848770 nit 1)."""
    if owner is None or sub.get("thread_id"):
        return False
    profile = str(sub.get("notifier_profile") or "")
    return not profile or profile == owner.profile


def gateway_claims(session_key: str, sub: dict) -> bool:
    """:func:`claims_row` against the chat's last :func:`legacy_gateway_owns` answer (the board poll's check)."""
    return claims_row(_owners.get(session_key), sub)


def _gateway_launch_profile(pid: int) -> str:
    """The live gateway's launch profile: the first ``served_profiles`` entry its runtime record names
    (``run_adapters._record_served_profiles``). Unknown: "", which matches only unstamped rows."""
    from gateway.status import read_runtime_status
    from hermes_constants import get_default_hermes_root
    runtime = read_runtime_status(get_default_hermes_root() / "gateway_state.json") or {}
    served = runtime.get("served_profiles")
    if runtime.get("pid") not in (None, pid) or not isinstance(served, list) or not served:
        return ""
    return str(served[0] or "")


def _lineage(session: dict, session_lineage) -> tuple:
    try:
        return tuple(session_lineage(str(session.get("session_key") or "")))
    except Exception:  # health: allow BLE001 -- no lineage: the chat's own id still answers
        return ()


def chat_lineage(session_key: str, session_lineage) -> tuple:
    """The chat and its compaction parents (``sibling_chats`` for its tui rows): its ``local`` rows sit on the
    lineage root. An unreadable lineage leaves the chat's own id."""
    return (session_key, *_lineage({"session_key": session_key}, session_lineage))


def legacy_gateway_owns(session: dict, session_lineage, session_db, now: Optional[float] = None) -> bool:
    """Whether the live gateway claims every tui notice this backend buffers for the chat, so handing them back
    reaches a leg that delivers them: :func:`gateway_owner` for the chat, :func:`claims_row` for each claim.

    The chat's answer is reused for ``OWNER_TTL_S``. A fresh one reads the ledger and the turn leases over the
    chat and its compaction parents (``session_lineage``) in ONE open of the session's store (``session_db``).
    A changed answer starts a new episode, logged once at INFO, so a long hand-back is visible in agent.log."""
    now = time.monotonic() if now is None else now
    key = str(session.get("session_key") or "")
    memo = session.get("_kb_gateway_owner")
    if memo is not None and now - memo[0] < OWNER_TTL_S:
        owner = memo[1]
    else:
        ids = tuple(dict.fromkeys(s for s in (key, *_lineage(session, session_lineage)) if s))
        try:
            owner = gateway_owner(ids, *_ledger_and_leases(session, ids, session_db))
        except Exception:  # health: allow BLE001 -- an unprovable owner keeps the legacy path
            owner = None
        session["_kb_gateway_owner"] = (now, owner)
        if owner != (memo[1] if memo is not None else None) and owner is not None:
            logger.info("kanban notices: chat %s goes to gateway pid %s (profile %s, %s); this backend hands it "
                        "the chat's tui notices for that profile", key, owner.pid, owner.profile or "?", owner.reason)
        elif owner is None and memo is not None and memo[1] is not None:
            logger.info("kanban notices: chat %s is back with this backend (gateway pid %s no longer holds it)",
                        key, memo[1].pid)
    if owner is None:
        _owners.pop(key, None)
        return False
    _owners[key] = owner
    claims = session.get("_kanban_claims") or []
    return all(claims_row(owner, c) for c in claims if not c.get("remote"))


def _ledger_and_leases(session: dict, ids: tuple, session_db) -> tuple:
    """(first ledger status over ``ids``, their turn-lease holders), read in one open of the session's store.
    The lease is read only when no ledger row answers (the common no-work case reads no lease)."""
    from tui_gateway.session_lifecycle import _CANONICAL_PENDING_SQL
    with session_db(session) as db:
        if db is None:
            raise RuntimeError("session store unavailable")
        status = None
        read_ctx = getattr(db, "_read_ctx", None)
        if read_ctx is not None:
            with read_ctx() as conn:
                for sid in ids:
                    row = conn.execute(_CANONICAL_PENDING_SQL, (sid, sid)).fetchone()
                    if row is not None:
                        status = str(row[0])
                        break
        if status is not None:
            return status, ()
        owner_of = getattr(db, "session_turn_lease_owner", None)
        holders = (str((owner_of(sid) or {}).get("holder") or "") for sid in ids) if owner_of else ()
        return None, tuple(h for h in holders if h)


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
