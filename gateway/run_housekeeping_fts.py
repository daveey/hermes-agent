"""Gateway housekeeping chores for the state.db full-text search index (moved out of gateway/run.py). [daveey]"""

import logging

# Same logger identity as gateway/run.py, where these chores lived.
logger = logging.getLogger("gateway.run")


def _housekeeping_deferred_fts_retry() -> None:
    """A SessionDB opened while another process held the rebuild lock fails closed onto the LIKE fallback
    and the gateway stays up for days. Non-blocking, rate-limited inside SessionDB; no-op when not stale."""
    # Retry here, on the existing tick, against the shared instances this process already holds:
    # non-blocking admission, no new thread, rate-limited inside SessionDB. No-op when nothing is stale (one
    # attribute read per instance). See #100108.
    from hermes_state_registry import borrow_live_shared_session_dbs
    with borrow_live_shared_session_dbs() as _session_dbs:
        for _sdb in _session_dbs:
            _retry = getattr(_sdb, "retry_deferred_fts_recovery", None)
            if callable(_retry) and _retry():
                logger.info(
                    "Deferred state.db FTS rebuild completed in-process for %s; full-text search restored.",
                    getattr(_sdb, "db_path", "state.db"))


def _housekeeping_fts_backfill() -> None:
    """Advance a claimed chunked FTS backfill (online recovery, interrupted optimize) a bounded time per tick on
    the shared instances; no-op without a claim. Each chunk is one short write transaction
    (hermes_state_fts_online)."""
    from hermes_state_registry import borrow_live_shared_session_dbs
    with borrow_live_shared_session_dbs() as _session_dbs:
        for _sdb in _session_dbs:
            _advance = getattr(_sdb, "advance_fts_backfill", None)
            if callable(_advance):
                _advance()
