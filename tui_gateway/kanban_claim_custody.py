"""Bound in-memory notice custody independently of turn admission."""
import contextlib

from hermes_cli.kanban_db_notify import NOTIFY_CLAIM_STALE_S


def release_expired(session, now):
    """Never let a busy/held chat renew an undelivered buffer indefinitely.

    Expiry is not the poller-stop release, which rewinds by cursor and would let this
    stale buffer clear a fresh claim of the same range taken after the sweeper recovered ours. Expiry instead
    asks the DB to release only a marker that is still ours AND itself stale; otherwise it just drops the
    in-memory copy."""
    claims = session.get("_kanban_claims") or []
    for claim in claims:
        started = claim.setdefault("claimed_mono", now)
        if not claim.get("durable") and now - started >= NOTIFY_CLAIM_STALE_S:
            _release_expired_claims(session)
            return True
    return False


def _release_expired_claims(session: dict) -> None:
    import logging
    logger = logging.getLogger(__name__)
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    subs = set()
    for claim in session.get("_kanban_claims") or []:
        if claim.get("durable") or not claim.get("texts"):
            continue
        if claim.get("remote"):
            try:
                from tui_gateway import kanban_remote_notices as _krn
                _krn.release(claim["remote"])
            except Exception:  # health: allow BLE001 -- expiry must always drop the buffer
                logger.warning("remote kanban notice release on expiry failed", exc_info=True)
            continue
        subs.add((claim["board"], claim["task_id"], claim["platform"], claim["chat_id"],
                  claim.get("thread_id") or ""))
    for board, task_id, platform, chat_id, thread_id in sorted(subs):
        try:
            with contextlib.closing(_kbc.connect(board=board)) as conn:
                _kbn.release_expired_notify_claim(conn, task_id=task_id, platform=platform, chat_id=chat_id,
                                                  thread_id=thread_id)
        except Exception:  # health: allow BLE001 -- expiry must always drop the buffer
            logger.warning("kanban notice expiry release failed for %s/%s", board, task_id, exc_info=True)
    session["_kanban_claims"] = [c for c in session.get("_kanban_claims", [])
                                 if c.get("durable") and not c.get("acked")]
    session["_kanban_pending"] = []


def fresh_texts(batch, delivered):
    """Deduplicate a reclaimed range against both transcript and buffered copies."""
    seen = set(delivered)
    fresh = []
    for text in batch:
        keys = set(getattr(text, "_kb_keys", ()) or ())
        if keys and keys <= seen:
            continue
        fresh.append(text)
        seen.update(keys)
    return fresh


def batch_text(texts) -> str:
    """The turn text for a buffered batch, each block notice's ask re-read against its card NOW (delivery).

    A notice is rendered when it is claimed, then may wait in ``_kanban_pending`` behind a busy turn while its
    card is unblocked or finished; ``refresh_stale_ask`` swaps such a stale "Next action for the user" ask for a
    ``Since then:`` line (still-blocked, remote and unreadable cards stay byte-identical). The texts themselves
    are not replaced: claims track them by identity and keep their ``_kb_*`` attributes. Never raises. [daveey]"""
    import logging

    from hermes_cli.kanban_stale_ask import ASK_MARK, refresh_stale_ask
    out = []
    for text in texts:
        try:
            out.append(refresh_stale_ask(str(text)) if ASK_MARK in text else str(text))
        except Exception:  # health: allow BLE001 -- a refresh must never cost the notice itself
            logging.getLogger(__name__).warning("stale block-ask refresh failed; delivering unchanged", exc_info=True)
            out.append(str(text))
    return "\n".join(out)


def release_claims(session: dict, log_failure, logger) -> None:
    """The poller is stopping with notices still buffered: hand their claimed ranges back (newest first, CAS) so
    the chat's next consumer delivers them now instead of after this process exits. Never raises; takes no
    lock (a turn may hold ``history_lock`` while the poller stops). [daveey]"""
    if not session.get("_kanban_claims") and not session.get("_kanban_pending"):
        return
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    for claim in reversed(session.get("_kanban_claims") or []):
        if claim.get("durable") or not claim.get("texts"):
            continue
        if claim.get("remote"):
            try:
                from tui_gateway import kanban_remote_notices as _krn
                _krn.release(claim["remote"])
            except Exception as exc:  # health: allow BLE001 -- preserve poller-stop best-effort release and existing stderr diagnostic
                log_failure("remote kanban notice release on poller stop failed", exc)
            continue
        try:
            with contextlib.closing(_kbc.connect(board=claim["board"])) as conn:
                _kbn.rewind_notify_cursor(
                    conn, task_id=claim["task_id"], platform=claim["platform"], chat_id=claim["chat_id"],
                    thread_id=claim.get("thread_id") or "", claimed_cursor=claim["new"], old_cursor=claim["old"])
        except Exception as exc:  # health: allow BLE001 -- preserve poller-stop best-effort release and existing stderr diagnostic
            log_failure("kanban notice release on poller stop failed", exc)
    # This process stops serving the chat: any marker it still holds for these subs (a durable claim never acked,
    # an earlier claim it lost track of) goes back too, or the chat's next consumer waits behind this live
    # process (t_01a199de3003e911). [daveey]
    subs = {(c["board"], c["task_id"], c["platform"], c["chat_id"], c.get("thread_id") or "")
            for c in session.get("_kanban_claims") or [] if not c.get("remote")}
    for board, task_id, platform, chat_id, thread_id in sorted(subs):
        try:
            with contextlib.closing(_kbc.connect(board=board)) as conn:
                _kbn.release_notify_claim(conn, task_id=task_id, platform=platform, chat_id=chat_id,
                                          thread_id=thread_id, why="stopped serving the chat")
        except Exception:
            logger.warning("kanban notice claim release on poller stop failed for %s/%s", board, task_id,
                           exc_info=True)
    session["_kanban_claims"] = []
    session["_kanban_pending"] = []
