"""Retry durable queued prompts after an update hold or transient contention."""
import threading


def retry_queued_prompt(rid, sid, session):
    """Called under history_lock; retain the envelope, not an admitted turn.

    One timer per session. Each attempt re-enters the normal admission fence.
    Backend shutdown/Stop may discard the queue; the callback checks it afresh.
    """
    from hermes_cli.backend_retirement import retirement
    # Retirement refusal keeps its original semantics: the client is replacing
    # this process, not waiting for an update hold/short filesystem contention.
    if not retirement.acquire():
        return False
    retirement.release()
    if session.get("_closing") or session.get("_finalized") or not session.get("queued_prompt"):
        return False
    if session.get("_admission_retry") is not None:
        return True

    def retry():
        from tui_gateway import server
        with session["history_lock"]:
            session.pop("_admission_retry", None)
            if session.get("_closing") or session.get("_finalized") or not session.get("queued_prompt"):
                return
        server._drain_queued_prompt(rid, sid, session)

    timer = threading.Timer(0.25, retry)
    timer.daemon = True
    session["_admission_retry"] = timer
    try:
        timer.start()
    except BaseException:
        session.pop("_admission_retry", None)
        raise
    return True
