"""Minted-draft materialization for ``session.resume`` (``methods_session`` split).

Moved from ``tui_gateway/methods_session.py`` (file-line ratchet): the bodies close over server.py
globals through ``method_ctx.bind_module`` exactly as before; publication runs from the parent's
``register()``. A minted key whose chat was deleted is terminal, never a draft (desktop-03cd4d73b3).
"""

from __future__ import annotations

from .method_ctx import bind_module


def _resume_materialize_minted(ctx: _Resume) -> None:
    """Row for a minted-but-never-persisted key (#96793): adopt the id, not 4007.

    Only for keys THIS backend's store recorded at ``session.create``
    (``was_session_key_minted``). The key shape is not proof of ownership: with
    several gateways (e.g. two Macs) every one mints the same shape, and a client
    probing this backend for another gateway's key must get 4007 so it routes the
    resume to the owner — materializing here fabricated empty, runtime-less rows.

    ``session.create`` mints the stored key but intentionally writes no state.db row
    until the first prompt (no "Untitled" litter). If the backend dies in that window,
    the key exists client-side (pinned tile, stored id) but has no row anywhere — and
    after a restart the in-memory live-lazy lookup above can't find it either, so the
    resume 4007ed forever. When the target is a well-formed server-minted key, mint
    the row here and let the normal resume path continue with empty history. Abandoned
    drafts still leave no row: nothing is written until a client explicitly resumes
    the exact key. ``create_session`` is an upsert, so a concurrently persisted row is
    not clobbered (only its NULL model/source columns would fill in).
    """
    try:
        ctx.db.create_session(
            ctx.target,
            source=_resolve_session_source(_str_param(ctx.params, "source") or None),
            model=_resolve_model(),
            profile_name=profile_name_for_home(ctx.profile_home) or _response_profile_name(ctx.profile),
        )
        ctx.found = ctx.db.get_session(ctx.target)
        logger.info(
            "materialized session row for minted-but-unpersisted key %s (resume no longer 4007s)",
            ctx.target,
        )
    except Exception as exc:
        # A delete that landed after the retirement preflight: the transactional resurrection fence
        # refuses the insert with ``not_found``. That is the deletion's normal terminal outcome.
        if getattr(exc, "reason", None) == "not_found" and _session_retired(ctx):
            ctx.retired = True
            logger.info("resume of %s: deleted while materializing; the deletion fence held", ctx.target)
            return
        logger.warning("failed to materialize session row for %s", ctx.target, exc_info=True)


def _session_retired(ctx: _Resume) -> bool:
    """True iff the SELECTED store holds the durable deletion fence for ``ctx.target``.

    Only the fence proves deletion: an absent row may be an unpersisted draft, and an unreadable store
    proves nothing, so a failed read answers False (the caller keeps its ordinary path)."""
    try:
        from hermes_state_mutation_retirement import retired_session
        return bool(retired_session(ctx.db, ctx.target))
    except Exception:
        logger.warning("retirement lookup failed for %s", ctx.target, exc_info=True)
        return False


def _was_minted_here(ctx: _Resume) -> bool:
    try:
        return bool(ctx.db.was_session_key_minted(ctx.target))
    except Exception:
        logger.warning("minted-key lookup failed for %s; not materializing", ctx.target, exc_info=True)
        return False


def _resume_materialize_or_refuse(ctx: _Resume) -> None:
    """A deleted chat keeps its mint record: its retirement fence, not the missing row, is the truth."""
    if _session_retired(ctx):
        ctx.retired = True
        logger.info("resume of %s: session was deleted (retired); not materializing a draft", ctx.target)
    else:
        _resume_materialize_minted(ctx)


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals)."""
    bind_module(globals(), server, skip=("_",))
