"""Kanban collection/binding boundary, rebound onto server globals. [daveey]"""
from .method_ctx import bind_module


def _notif_poll_kanban_attempt(sid: str, session: dict, *, remote_only=False) -> None:
    # Collection only reads/claims local SQLite rows. Resolve the home each time;
    # provider/terminal bindings are needed only for delivery.
    from tui_gateway.notification_idle import collection_home
    with collection_home(session, _launch_home):
        from tui_gateway.kanban_claim_custody import release_expired
        if release_expired(session, time.monotonic()):
            return
        if session.get("_finalized") or session.get("_closing") or session.get("_lease_taken_over"):
            prepared = ([], [])
        else:
            prepared = _notif_collect_kanban(session, remote_only=True) if remote_only else _notif_collect_kanban(session)
        # Track custody before provider binding or a concurrent finalize can fail.
        session.setdefault("_kanban_pending", []).extend(prepared[0])
        session.setdefault("_kanban_claims", []).extend(prepared[1])
    if not any(prepared) and not session.get("_kanban_pending") and not session.get("_kanban_claims"):
        return
    with contextlib.ExitStack() as scope:
        try:
            scope.enter_context(_session_profile_runtime_scope(session))
        except Exception as exc:
            # Nothing dispatched: return custody so the next normal tick can retry.
            from tui_gateway.notification_remote_retry import cancel
            cancel(session)
            with collection_home(session, _launch_home):
                _kb_release_claims(session)
            logger.debug("Kanban provider binding failed", exc_info=True)
            _notif_log_failure("kanban provider binding failed", exc)
            return
        _notif_poll_kanban_scoped(sid, session, prepared)


def register(server):
    bind_module(globals(), server)
