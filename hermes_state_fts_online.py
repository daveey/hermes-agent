"""Online stale-FTS recovery: rebuild the search indexes while other processes keep state.db open. [daveey]

The one-shot rebuild (``_recover_stale_fts_locked``) drops and rebuilds both FTS indexes in one write
transaction. It therefore waits for every other holder of state.db to leave (``_defer_stale_fts_for_holders``),
and on a damaged index the DROP itself fails. On the hub (2026-11-18) both held at once: supervised services
always held the file (the deferral went "futile" after 30 min and stayed so), and the trigram index had pages
referenced twice, so ``DROP``/``'delete-all'``/``'rebuild'`` all raised "database disk image is malformed".
Search ran on the LIKE fallback for hours, and only a whole-store recovery with every holder stopped could fix it.

This path never holds the write lock for long and never frees a page it cannot prove is unshared:

1. Plan (read-only, no lock): one whole-file ``PRAGMA integrity_check``. A scoped check of one table cannot see a
   page it shares with another structure, and freeing such a page (DROP, or deleting its rows) would corrupt the
   other owner. So: errors in a canonical tree or the freelist -> refuse (``hermes sessions recover`` territory);
   errors only in an index family's trees -> that family is *unlinked*; a family with no error is *trashed*.
2. One write transaction (milliseconds): drop the FTS triggers and source views; for an unlinked family delete
   its vtable and shadow-table rows from ``sqlite_master`` (``writable_schema``: its pages are never read and stay
   allocated, reported by integrity_check as "Page N: never used" until a VACUUM or ``hermes sessions recover``);
   for a trashed family delete only its vtable row and rename its shadow tables to ``fts_v22_trash_*``, which the
   existing chunked teardown empties and drops (pages freed normally); recreate both indexes with the shipping
   DDL; claim the chunked backfill (``fts_rebuild_high_water`` = MAX(messages.id), progress 0); clear
   ``fts_stale``. From the commit on, the triggers index every new message (their window gate admits ids above
   the high water).
3. :meth:`advance_fts_backfill`, from the gateway housekeeping tick, runs ``fts_rebuild_step`` chunks and then the
   trash teardown for a bounded time per tick, each chunk one short transaction sized to
   ``_FTS_REBUILD_CHUNK_TARGET_S``. Until it finishes, search covers the not-yet-indexed window with the existing
   LIKE gap supplement.

A healthy index (the futile-holder case) therefore ends with a clean file. Only an index that integrity_check
names as damaged leaves unreachable pages, and those stores already failed every whole-file check before the
recovery. Unlinking rather than renaming a damaged index is deliberate: a damaged index kept under any name can
later be dropped or drained, and that frees pages that live data may already reuse (the double references).

Used automatically by :meth:`retry_deferred_fts_recovery` once the one-shot rebuild is futile (same holders for
``_FTS_HOLDER_FUTILE_SECONDS``) or failed with a corrupt-image error, and by ``hermes sessions repair-search``.
Never on a read-only or quarantined handle, nor on the legacy inline (pre-v23) layout.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import time
from typing import Optional

from hermes_state_common import (FTS_REBUILD_DEFERRAL_KEY, FTS_SQL, FTS_STALE_KEY, FTS_STORAGE_VERSION,
                                 FTS_TRIGRAM_SQL, _FTS_TRIGGERS, fts_rebuild_admission)

logger = logging.getLogger("hermes_state")

# Wall time one housekeeping tick may spend on backfill / teardown chunks (each chunk its own short transaction).
FTS_BACKFILL_TICK_BUDGET_S = 5.0
# A refused plan (damage outside the index) is not re-checked by the automatic retry for this long.
ONLINE_REFUSAL_MEMO_S = 6 * 3600.0
FAMILIES = ("messages_fts", "messages_fts_trigram")
TRASH_PREFIX = "fts_v22_trash_"  # the optimize-storage teardown drains every table with this prefix
# state_meta: JSON list of trash tables a whole-file plan proved unshared (the online recovery's own trash, or
# trash a clean plan vouched for before its first teardown chunk). Only these are drained.
TRASH_VERIFIED_KEY = "fts_trash_verified"


def verified_trash(conn) -> set:
    """The trash table names recorded under ``TRASH_VERIFIED_KEY`` (empty for a missing or unreadable value)."""
    row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (TRASH_VERIFIED_KEY,)).fetchone()
    try:
        names = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        return set()
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def write_verified_trash(conn, names) -> None:
    """Replace the recorded list; caller holds the write transaction."""
    if names:
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (TRASH_VERIFIED_KEY, json.dumps(sorted(names))))
    else:
        conn.execute("DELETE FROM state_meta WHERE key = ?", (TRASH_VERIFIED_KEY,))


_FAMILY_OBJECT_RE = re.compile(r"(messages_fts(?:_trigram)?)(?:_data|_idx|_content|_docsize|_config)?")


def family_of(name: str) -> Optional[str]:
    """The FTS index family a vtable or shadow-table name belongs to; None for anything else (full match on the
    FTS5 shadow suffixes, so a lookalike table is never swept in)."""
    m = _FAMILY_OBJECT_RE.fullmatch(name or "")
    return m.group(1) if m else None


class OnlineRecoveryRefused(Exception):
    """The plan step found a reason not to touch the store."""


_CHECK_LIMIT = 100_000


def plan_from_check(lines, master) -> dict:
    """The decision of :func:`plan_online_recovery` from integrity_check output and the ``(rootpage, type, name)``
    rows of the same snapshot. Raises :class:`OnlineRecoveryRefused` unless every problem line is attributable.

    Safety rests on how SQLite reports a shared page: the free-page list is walked first and every page is named
    against the SECOND structure that reaches it. So a family not named by any line shares no page with anything
    reached after it, and anything reached before it was named itself: a canonical tree (refused here), the other
    family (unlinked, never read again) or nothing. Draining an unnamed family therefore frees only its own pages.
    That argument needs every line mapped and the walk complete, hence the refusals below."""
    from hermes_state_repair import _INTEGRITY_TREE_RE, integrity_damage_is_structural

    problems = [line for line in lines if line not in ("ok", "*** in database main ***")]
    if len(problems) >= _CHECK_LIMIT - 1:
        raise OnlineRecoveryRefused(f"integrity_check stopped at its {_CHECK_LIMIT}-error limit; later trees unchecked")
    if integrity_damage_is_structural(problems, master):
        raise OnlineRecoveryRefused("integrity_check reports damage outside the search indexes: "
                                    + "; ".join(problems[:3]))
    by_root = {int(rp): name for rp, _type, name in master}
    damaged = set()
    for line in problems:
        tree = _INTEGRITY_TREE_RE.search(line)
        if not tree:
            continue  # "Page N: never used", FTS5 xIntegrity blob lines: no page is shared
        name = by_root.get(int(tree.group(1)))
        family = family_of(name or "")
        if family is None:  # Tree 1 (sqlite_schema) or a root this snapshot cannot name
            raise OnlineRecoveryRefused(f"integrity_check names a tree outside the search indexes: {line}")
        damaged.add(family)
    return {"unlink": sorted(damaged), "trash": [f for f in FAMILIES if f not in damaged], "problems": len(problems),
            "roots": {name: int(rp) for rp, _type, name in master if family_of(name)}}


def plan_online_recovery(db_path) -> dict:
    """Step 1: ``{"unlink": [...], "trash": [...], "problems": n, "roots": {...}}`` from one read-only whole-file
    integrity_check and the schema of the same read snapshot (see :func:`plan_from_check`). Raises
    :class:`OnlineRecoveryRefused` when it cannot prove the plan safe."""
    from hermes_state_holders import read_only_db_uri

    from hermes_cli.sqlite_safe_read import connect_tracked

    try:
        conn = connect_tracked(read_only_db_uri(db_path), uri=True, timeout=30.0, isolation_level=None,
                               tracking_path=db_path)
    except sqlite3.Error as exc:
        raise OnlineRecoveryRefused(f"cannot open state.db read-only: {exc}") from exc
    try:
        conn.execute("BEGIN")  # one read snapshot for the schema and the check
        master = [tuple(r) for r in conn.execute("SELECT rootpage, type, name FROM sqlite_master WHERE rootpage > 0")]
        lines = [line for r in conn.execute(f"PRAGMA integrity_check({_CHECK_LIMIT})")
                 for line in str(r[0]).splitlines()]
        conn.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        raise OnlineRecoveryRefused(f"integrity_check could not complete: {exc}") from exc
    finally:
        conn.close()
    return plan_from_check(lines, master)


def _sql_names(names) -> str:
    return ", ".join("'" + n.replace("'", "''") + "'" for n in names)


def online_recovery_script(*, unlink_tables, trash, trash_tables, trigram: bool, stamp: int) -> str:
    """Step 2 as one transaction (see the module docstring). *unlink_tables*: every table (vtable and shadow) of the
    unlinked families; their rows and those of any index on them are deleted from sqlite_master. *trash*: the
    trashed families, whose vtable rows are deleted and whose shadow tables (*trash_tables*) are renamed to
    ``fts_v22_trash_<name>_<stamp>``."""
    parts = ["BEGIN IMMEDIATE;"]
    parts += [f"DROP TRIGGER IF EXISTS {t};" for t in _FTS_TRIGGERS]
    parts += ["DROP VIEW IF EXISTS messages_fts_trigram_src;", "DROP VIEW IF EXISTS messages_fts_src;",
              "PRAGMA writable_schema=ON;"]
    if unlink_tables:
        parts.append(f"DELETE FROM sqlite_master WHERE type IN ('table', 'index') "
                     f"AND tbl_name IN ({_sql_names(unlink_tables)});")
    if trash:
        parts.append(f"DELETE FROM sqlite_master WHERE type = 'table' AND name IN ({_sql_names(trash)}) "
                     "AND sql LIKE 'CREATE VIRTUAL TABLE%';")
    parts.append("PRAGMA writable_schema=RESET;")
    parts += [f"ALTER TABLE {name} RENAME TO {TRASH_PREFIX}{name}_{stamp};" for name in trash_tables]
    if trash_tables:
        # The plan proved these unshared: their teardown needs no new plan (merged into the recorded list here, in
        # the same transaction as the rename).
        names = json.dumps(sorted(f"{TRASH_PREFIX}{name}_{stamp}" for name in trash_tables)).replace("'", "''")
        parts.append(f"INSERT INTO state_meta (key, value) VALUES ('{TRASH_VERIFIED_KEY}', '{names}') "
                     "ON CONFLICT(key) DO UPDATE SET value = (SELECT json_group_array(value) FROM ("
                     "SELECT value FROM json_each(CASE WHEN json_valid(state_meta.value) THEN state_meta.value "
                     "ELSE '[]' END) UNION SELECT value FROM json_each(excluded.value)));")
    # The claim lands before the triggers exist, so no row is ever both trigger-indexed and backfilled.
    parts += ["INSERT INTO state_meta (key, value) SELECT 'fts_rebuild_high_water', CAST(COALESCE(MAX(id), 0) AS TEXT) "
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

    def _fts_plan_outcome(self):
        """``("ok", plan)`` or ``("refused", reason)`` from a fresh whole-file check, honouring the refusal memo."""
        try:
            return ("ok", self._fts_plan())
        except OnlineRecoveryRefused as exc:
            return ("refused", str(exc))

    def _fts_plan(self, *, force: bool = False) -> dict:
        """The plan for the current recovery attempt: the retry tick's own (``_fts_tick_plan``, made before it took
        ``self._lock`` and discarded at the end of the tick), else a fresh whole-file check. Never a plan from an
        earlier attempt: damage can appear between attempts. Raises OnlineRecoveryRefused, also for a refusal
        remembered within ``ONLINE_REFUSAL_MEMO_S`` (unless *force*)."""
        tick = getattr(self, "_fts_tick_plan", None)
        if tick is not None and not force:
            if tick[0] == "refused":
                raise OnlineRecoveryRefused(tick[1])
            return tick[1]
        now = time.monotonic()
        if not force and now < getattr(self, "_fts_online_refused_until", 0.0):
            raise OnlineRecoveryRefused(getattr(self, "_fts_online_refused_reason", "a recent plan refused"))
        try:
            return plan_online_recovery(self.db_path)
        except OnlineRecoveryRefused as exc:
            self._fts_online_refused_until = now + ONLINE_REFUSAL_MEMO_S
            self._fts_online_refused_reason = str(exc)
            raise

    # ── Gate for every other path that frees an FTS tree wholesale ──
    # DROP, FTS5 'rebuild' / 'delete-all' and a trash teardown free every page of a tree. On the hub's damage shape
    # (a page referenced twice) that frees the shared page twice, and SQLite later hands it to canonical data. So
    # each such path asks this gate first, with the same whole-file plan the online recovery uses.

    @contextlib.contextmanager
    def _fts_runtime_plan(self, needed: bool = True):
        """Make the plan now, before the caller takes ``self._lock``, for this call only (like the retry tick's
        ``_fts_tick_plan``). An enclosing attempt's plan stands; *needed* False makes none."""
        if not needed or getattr(self, "_fts_tick_plan", None) is not None:
            yield
            return
        self._fts_tick_plan = self._fts_plan_outcome()
        try:
            yield
        finally:
            self._fts_tick_plan = None

    def _fts_wholesale_free_allowed(self, families, *, context: str, cursor=None) -> bool:
        """True when freeing the trees of *families* (FTS index families; empty for trash teardown) is provably safe.

        False when the plan refuses (damage it cannot attribute: logged, nothing changes) or names one of
        *families* (the store goes to the fail-open state with ``_fts_oneshot_failed`` set, so the retry tick's
        online path unlinks the family unread). Uses the attempt's plan (``_fts_tick_plan``); without one it
        plans inline only when ``self._lock`` is free (open-time schema init, or a caller outside the lock) and
        otherwise skips: a whole-file check takes minutes and every write of this process waits on that lock.
        *cursor*: where the fail-open state is written (default: the writer connection; caller holds the lock)."""
        startup = getattr(self, "_fts_startup_init", False)
        if getattr(self, "_fts_tick_plan", None) is None and not startup and self._lock.locked():
            logger.warning("%s skipped: no integrity plan was made for this attempt before the session lock; a later "
                           "attempt plans first.", context)
            return False
        try:
            plan = self._fts_plan()
        except OnlineRecoveryRefused as exc:
            logger.error("%s skipped: %s. `hermes sessions recover` rebuilds the store offline.", context, exc)
            return False
        if startup:
            self._fts_tick_plan = ("ok", plan)  # one whole-file check per open (cleared when schema init ends)
        named = [f for f in families if f in plan["unlink"]]
        if not named:
            return True
        self._fts_oneshot_failed = True
        self._detach_fts_stale(cursor if cursor is not None else self._conn.cursor())
        logger.error("%s skipped: integrity_check names damaged index tree(s) %s, which freeing could release twice. "
                     "Search is detached (LIKE fallback); the retry recovers online (unlinks them unread).",
                     context, ", ".join(named))
        return False

    def _fts_verify_trash(self, names) -> set:
        """Record *names* (trash tables no plan has vouched for) as verified when a fresh plan, made here outside
        ``self._lock``, is clean; returns the names recorded. A plan names a trash table only by refusing (it is
        no index family), so a damaged trash table is never drained."""
        with self._fts_runtime_plan():
            if not self._fts_wholesale_free_allowed((), context=f"Teardown of FTS trash {', '.join(sorted(names))}"):
                return set()

        def _record(conn):
            present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? "
                                                  "ESCAPE '\\'", (TRASH_PREFIX.replace("_", "\\_") + "%",))}
            keep = (verified_trash(conn) | set(names)) & present
            write_verified_trash(conn, keep)
            return keep & set(names)

        return self._execute_write(_record)

    def recover_stale_fts_online(self, *, reason: str, force: bool = False) -> bool:
        """Steps 1-2. True when done (or already done by a peer). Never raises; False leaves everything as it was.
        *force*: run on a store that is not marked stale (operator)."""
        if getattr(self, "read_only", False) or self._conn is None:
            return False
        quarantine = self._quarantine_reason()
        if quarantine is not None:
            logger.error("Online FTS recovery refused on a quarantined state.db handle (%s).", quarantine)
            return False
        try:
            with self._lock:
                cursor = self._conn.cursor()
                if not force and self._adopt_peer_fts_state_locked(cursor):
                    return True  # not stale (any more): a peer recovered
                if self._db_has_legacy_inline_fts(cursor):
                    logger.warning("Online FTS recovery skipped: legacy inline FTS layout (run "
                                   "`hermes sessions optimize-storage`).")
                    return False
            try:
                plan = self._fts_plan(force=force)
            except OnlineRecoveryRefused as exc:
                logger.error("Online FTS recovery refused (%s): %s. `hermes sessions recover` rebuilds the store "
                             "offline.", reason, exc)
                return False
            with fts_rebuild_admission(self.db_path, timeout_seconds=0.0) as admitted:
                if not admitted:
                    return False  # another process holds the rebuild authority; the next tick retries
                with self._lock:
                    if self._conn is None:
                        return False
                    cursor = self._conn.cursor()
                    # The plan ran without the lock: a peer may have recovered meanwhile (its new tables must not get
                    # this plan), or the index objects may have moved. Re-check both under the authority.
                    if not force and self._adopt_peer_fts_state_locked(cursor):
                        return True
                    roots = {r[0]: int(r[1]) for r in cursor.execute(
                        "SELECT name, rootpage FROM sqlite_master WHERE rootpage > 0").fetchall() if family_of(r[0])}
                    if roots != plan["roots"]:
                        logger.info("Online FTS recovery: the index objects changed since the plan; the next retry "
                                    "plans again.")
                        return False
                    trigram = self._trigram_tokenizer_available(cursor)
                    tables = [(r[0], (r[1] or "").startswith("CREATE VIRTUAL")) for r in cursor.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()]
                    unlink_tables = [n for n, _v in tables if family_of(n) in plan["unlink"]]
                    trash_tables = [n for n, virtual in tables if family_of(n) in plan["trash"] and not virtual]
                    started = time.monotonic()
                    try:
                        cursor.executescript(online_recovery_script(
                            unlink_tables=unlink_tables, trash=plan["trash"], trash_tables=trash_tables,
                            trigram=trigram, stamp=int(time.time())))
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
                "Online FTS recovery (%s): search indexes recreated in %.3fs (unlinked damaged: %s; old pages "
                "drained in chunks: %s); backfilling %s messages in chunks.%s",
                reason, time.monotonic() - started, ", ".join(plan["unlink"]) or "none",
                ", ".join(plan["trash"]) or "none", high_water[0] if high_water else "?",
                " The damaged index's pages stay allocated until a VACUUM or `hermes sessions recover` "
                "(integrity_check reports them as never used)." if plan["unlink"] else "")
            return True
        except Exception:
            logger.error("Online FTS recovery failed (%s).", reason, exc_info=True)
            return False

    def _adopt_peer_fts_state_locked(self, cursor: sqlite3.Cursor) -> bool:
        """No ``fts_stale`` in state_meta: another process rebuilt or recovered the indexes; take its schema as this
        handle's state. True when the store is not stale. Caller holds ``self._lock``."""
        if cursor.execute("SELECT 1 FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone() is not None:
            return False
        base = self._fts_table_exists("messages_fts")
        self._fts_stale = False
        self._fts_enabled = base
        self._trigram_available = base and self._fts_table_exists("messages_fts_trigram")
        self._fts_deferred_holder_pids = None
        return True

    def fts_online_work_pending(self) -> bool:
        """A backfill claim or old shadow tables awaiting teardown."""
        if self.get_meta("fts_rebuild_high_water") is not None:
            return True
        with self._read_ctx() as conn:
            return self._has_fts_trash(conn)

    def advance_fts_backfill(self, budget_s: float = FTS_BACKFILL_TICK_BUDGET_S) -> bool:
        """Step 3: backfill chunks, then trash teardown chunks, for up to *budget_s* seconds; True while work remains.
        Never raises (a failed chunk logs and reports work remaining). No-op on a stale, read-only, quarantined or
        FTS-disabled handle, and on the legacy inline layout."""
        if getattr(self, "read_only", False) or self._conn is None or self._fts_stale or not self._fts_enabled:
            return False
        if self._quarantine_reason() is not None:
            return False
        try:
            with self._lock:
                if self._db_has_legacy_inline_fts(self._conn.cursor()):
                    return False
                # A step indexes the trigram half only when this handle believes it exists: re-probe so a handle
                # whose flags predate a peer's recovery never advances progress past rows it left out.
                self._trigram_available = self._fts_table_exists("messages_fts_trigram")
            deadline = time.monotonic() + max(float(budget_s), 0.0)
            more = self.fts_online_work_pending()
            while more and time.monotonic() < deadline:
                t0 = time.monotonic()
                if self.get_meta("fts_rebuild_high_water") is not None:
                    self.fts_rebuild_step()
                elif not self._fts_teardown_trash_step():
                    more = self.fts_online_work_pending()  # left: trash no clean plan vouches for (logged)
                    break
                self._fts_chunk_rows = self._next_chunk_rows(
                    self._fts_chunk_rows, time.monotonic() - t0, self._FTS_REBUILD_CHUNK_TARGET_S)
                more = self.fts_online_work_pending()
            return bool(more)
        except sqlite3.OperationalError as exc:
            logger.info("FTS backfill chunk deferred (%s); the next tick continues.", exc)
            return True
        except Exception:
            logger.warning("FTS backfill chunk failed; the next tick retries.", exc_info=True)
            return True
