"""Which leg holds a chat's tui Kanban notices: the canonical gateway, the chat's waking ``local`` row, or this
legacy Desktop backend (t_01a1cfbe6c8d46e7). [daveey]

Chat 20261026_235722_bef6c6 on the MacBook, 2026-11-12: t_01a1cfb6db097701's completion reached the chat twice
(the local wake at 04:40:25, the tui claim the busy backend had buffered at 04:59:20), and t_01a1ce412fbf6e7f's
waited 45 min, held in flight by a backend whose turns the gateway-owned chat refused.
"""
import contextlib
import logging

logger = logging.getLogger(__name__)


def gateway_owns(session: dict, ledger_refusal) -> bool:
    """The canonical gateway runs this chat now: its durable ledger holds work for it that ends on its own
    (``owner_forward.FORWARDABLE_LEDGER_STATES``), the condition under which ``gateway.kanban_tui_owner`` claims the
    chat's tui rows and admits them to its FIFO within one notifier tick. ``ledger_refusal`` is
    ``session_lifecycle._canonical_ledger_refusal``. An ``unknown`` admission, no ledger row or an unreadable
    ledger answer False: this backend keeps the rows as before (a legacy-only chat buffers while busy)."""
    try:
        refusal = ledger_refusal(session)
    except Exception:  # health: allow BLE001 -- an unprovable owner keeps the legacy path
        return False
    from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES
    return getattr(refusal, "ledger_status", None) in FORWARDABLE_LEDGER_STATES


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
                row = conn.execute("SELECT inflight_from FROM kanban_notify_subs WHERE task_id = ? AND platform = ? "
                                   "AND chat_id = ? AND thread_id = ?", tuple(ident.values())).fetchone()
                settled = int(row[0]) if row is not None and row[0] is not None else int(claim["old"])
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
                else:
                    claim["durable"] = True  # an earlier claim is still pending: the next ack run covers it
        except Exception as exc:  # health: allow BLE001 -- unreadable: the batch delivers as before
            log_failure("kanban held-claim custody check failed", exc)
    if dropped:
        logger.info("kanban notices: %d buffered notice(s) for %s were delivered by the chat's wake; resolved",
                    len(dropped), session.get("session_key"))
        with session["history_lock"]:
            session["_kanban_pending"] = [t for t in session.get("_kanban_pending") or [] if id(t) not in dropped]
