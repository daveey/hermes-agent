"""Online stale-FTS recovery: rebuild the search indexes while other processes keep state.db open. [daveey]

The one-shot rebuild (``_recover_stale_fts_locked``) drops and rebuilds both FTS indexes in one write
transaction. It therefore waits for every other holder of state.db to leave (``_defer_stale_fts_for_holders``),
and on a damaged index the DROP itself fails. On the hub (2026-11-18) both held at once: supervised services
always held the file (the deferral went "futile" after 30 min and stayed so), and the trigram index had pages
referenced twice, so ``DROP``/``'delete-all'``/``'rebuild'`` all raised "database disk image is malformed".
Search ran on the LIKE fallback for hours, and only a whole-store recovery with every holder stopped could fix it.

This path never reads the old index pages and never holds the write lock for long:

1. One write transaction (milliseconds): drop the FTS triggers and source views; delete every ``messages_fts*``
   table's rows from ``sqlite_master`` (``writable_schema``: the old pages stay allocated but unreachable);
   recreate both indexes empty with the shipping DDL; claim the chunked backfill (``fts_rebuild_high_water`` =
   MAX(messages.id), ``fts_rebuild_progress`` = 0); clear ``fts_stale``. From the commit on, the triggers index
   every new message (their window gate admits ids above the high water).
2. :meth:`advance_fts_backfill`, called from the gateway housekeeping tick, runs ``fts_rebuild_step`` chunks for a
   bounded time per tick, each chunk one short transaction sized to ``_FTS_REBUILD_CHUNK_TARGET_S``. Until it
   finishes, search covers the not-yet-indexed window with the existing LIKE gap supplement.

Cost: the unreachable pages are reported by ``PRAGMA integrity_check`` as "Page N: never used" (not structural
damage: ``integrity_damage_is_structural`` ignores them) and are reclaimed only by a VACUUM or
``hermes sessions recover`` at a planned moment. Unlinking rather than renaming is deliberate: a damaged index
renamed aside can later be dropped by the orphan-shadow sweep, and dropping it frees pages that live data may
already reuse (the double references); an unlinked one is reachable by nothing.

Used automatically by :meth:`retry_deferred_fts_recovery` once the one-shot rebuild is futile (same holders for
``_FTS_HOLDER_FUTILE_SECONDS``) or has failed with a database error, and by ``hermes sessions repair-search``.
Never on a read-only or quarantined handle, nor on the legacy inline (pre-v23) layout.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time

from hermes_state_common import (FTS_REBUILD_DEFERRAL_KEY, FTS_SQL, FTS_STALE_KEY, FTS_STORAGE_VERSION,
                                 FTS_TRIGRAM_SQL, _FTS_TRIGGERS, fts_rebuild_admission)

logger = logging.getLogger("hermes_state")

# Every FTS index object: the vtables, their shadow tables and any index on them (``tbl_name`` covers both).
# The source views are dropped by name; ``fts_v22_trash_*`` tables are not touched (optimize drains them).
_FTS_FAMILY_SQL = ("type IN ('table', 'index') AND (tbl_name IN ('messages_fts', 'messages_fts_trigram') "
                   "OR tbl_name LIKE 'messages\\_fts\\_%' ESCAPE '\\')")
# Wall time one housekeeping tick may spend on backfill chunks (each chunk is its own short transaction).
FTS_BACKFILL_TICK_BUDGET_S = 2.0
ONLINE_RECOVERY_EVENT = "fts_online_recovery"


def online_recovery_script(*, trigram: bool) -> str:
    """The single-transaction script of step 1 (see the module docstring)."""
    parts = ["BEGIN IMMEDIATE;"]
    parts += [f"DROP TRIGGER IF EXISTS {t};" for t in _FTS_TRIGGERS]
    parts += ["DROP VIEW IF EXISTS messages_fts_trigram_src;", "DROP VIEW IF EXISTS messages_fts_src;",
              "PRAGMA writable_schema=ON;", f"DELETE FROM sqlite_master WHERE {_FTS_FAMILY_SQL};",
              "PRAGMA writable_schema=RESET;",
              # The claim lands before the triggers exist, so no row is ever both trigger-indexed and backfilled.
              "INSERT INTO state_meta (key, value) SELECT 'fts_rebuild_high_water', CAST(COALESCE(MAX(id), 0) AS TEXT) "
              "FROM messages WHERE true ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
              "INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_progress', '0') "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
              FTS_SQL, FTS_TRIGRAM_SQL if trigram else "",
              f"INSERT INTO state_meta (key, value) VALUES ('fts_storage_version', '{FTS_STORAGE_VERSION}') "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
              f"DELETE FROM state_meta WHERE key IN ('{FTS_STALE_KEY}', '{FTS_REBUILD_DEFERRAL_KEY}', "
              "'fts_tool_full_content_high_water');",
              "COMMIT;"]
    return "\n".join(p for p in parts if p)


class SessionFtsOnlineRecoveryMixin:
    """See the module docstring. Mixed into ``SessionDB``."""

    def recover_stale_fts_online(self, *, reason: str) -> bool:
        """Step 1: unlink and recreate both FTS indexes in one short transaction and claim the chunked backfill.
        True when done (or already done by a peer). Never raises; False leaves everything as it was."""
        if getattr(self, "read_only", False) or self._conn is None:
            return False
        quarantine = self._quarantine_reason()
        if quarantine is not None:
            logger.error("Online FTS recovery refused on a quarantined state.db handle (%s).", quarantine)
            return False
        try:
            with fts_rebuild_admission(self.db_path, timeout_seconds=0.0) as admitted:
                if not admitted:
                    return False  # another process holds the rebuild authority; the next tick retries
                with self._lock:
                    if self._conn is None:
                        return False
                    cursor = self._conn.cursor()
                    if cursor.execute("SELECT 1 FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone() is None \
                            and not reason.startswith("operator"):
                        self._adopt_peer_fts_state_locked(cursor)
                        return True  # a peer already recovered
                    if self._db_has_legacy_inline_fts(cursor):
                        logger.warning("Online FTS recovery skipped: legacy inline FTS layout (run "
                                       "`hermes sessions optimize-storage`).")
                        return False
                    trigram = self._trigram_tokenizer_available(cursor)
                    started = time.monotonic()
                    try:
                        cursor.executescript(online_recovery_script(trigram=trigram))
                    except sqlite3.Error:
                        with contextlib.suppress(sqlite3.Error):
                            self._conn.rollback()
                        logger.error("Online FTS recovery failed; nothing changed (%s).", reason, exc_info=True)
                        return False
                    self._fts_stale = False
                    self._fts_enabled = True
                    self._trigram_available = trigram
                    self._fts_oneshot_failed = False
                    self._fts_deferred_holder_pids = None
                    self._fts_stale_retry_interval = 0.0
                    self._fts_chunk_rows = self._FTS_REBUILD_CHUNK_ROWS
                    high_water = cursor.execute(
                        "SELECT value FROM state_meta WHERE key = 'fts_rebuild_high_water'").fetchone()
                logger.warning(
                    "Online FTS recovery (%s): search indexes unlinked and recreated in %.3fs; backfilling %s "
                    "messages in chunks. The old index pages stay allocated until a VACUUM or `hermes sessions "
                    "recover` (integrity_check reports them as never used).",
                    reason, time.monotonic() - started, high_water[0] if high_water else "?")
                return True
        except Exception:
            logger.error("Online FTS recovery failed (%s).", reason, exc_info=True)
            return False

    def _adopt_peer_fts_state_locked(self, cursor: sqlite3.Cursor) -> bool:
        """Another process cleared ``fts_stale`` (it rebuilt or ran the online recovery): take its schema as this
        handle's state. True when the indexes are usable. Caller holds ``self._lock``."""
        if cursor.execute("SELECT 1 FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone() is not None:
            return False
        base = self._fts_table_exists("messages_fts")
        self._fts_stale = False
        self._fts_enabled = base
        self._trigram_available = base and self._fts_table_exists("messages_fts_trigram")
        self._fts_deferred_holder_pids = None
        return base

    def advance_fts_backfill(self, budget_s: float = FTS_BACKFILL_TICK_BUDGET_S) -> bool:
        """Step 2: run chunked backfill steps for up to *budget_s* seconds; True while work remains. Never raises.
        No-op without a backfill claim (``fts_rebuild_high_water``), on a stale, read-only or quarantined handle."""
        if getattr(self, "read_only", False) or self._conn is None or self._fts_stale or not self._fts_enabled:
            return False
        if self._quarantine_reason() is not None:
            return False
        try:
            if self.get_meta("fts_rebuild_high_water") is None:
                return False
            with self._lock:
                # A step indexes the trigram half only when this handle believes it exists: re-probe so a handle
                # whose flags predate a peer's recovery never advances progress past rows it left out.
                self._trigram_available = self._fts_table_exists("messages_fts_trigram")
            deadline = time.monotonic() + max(float(budget_s), 0.0)
            more = True
            while more and time.monotonic() < deadline:
                t0 = time.monotonic()
                more = self.fts_rebuild_step()
                self._fts_chunk_rows = self._next_chunk_rows(
                    self._fts_chunk_rows, time.monotonic() - t0, self._FTS_REBUILD_CHUNK_TARGET_S)
            return bool(more)
        except sqlite3.OperationalError as exc:
            logger.info("FTS backfill chunk deferred (%s); the next tick continues.", exc)
            return True
        except Exception:
            logger.warning("FTS backfill chunk failed; the next tick retries.", exc_info=True)
            return True
