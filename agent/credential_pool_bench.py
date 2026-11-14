"""Pool bench-lift diagnostics and rotation-local exclusions."""
import logging
import time

logger = logging.getLogger(__name__)


def log_early_lift(entry, mechanism):
    from agent.credential_pool_timestamps import _parse_absolute_timestamp
    reset = _parse_absolute_timestamp(entry.last_error_reset_at)
    if entry.last_status == "exhausted" and reset is not None and reset > time.time():
        logger.info("credential pool: bench lifted early %s mechanism=%s old_reset=%s",
                    entry.label or entry.id[:8], mechanism, reset)


def resync_with_logging(pool, entry):
    synced = pool._resync_stale_entry(entry)
    if synced.last_status != entry.last_status:
        log_early_lift(entry, "resync")
    return synced


def rotation_candidates(pool, exclude_id):
    return (entry for entry in pool._enabled_entries() if entry.id != exclude_id)
