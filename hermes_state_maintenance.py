"""Retention pruning, stale-session archiving and VACUUM policy mixin for SessionDB."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_state_common import (
    AUTO_VACUUM_MIN_FREELIST_RATIO, _id_chunks, _non_continuation_child_sql, _placeholders, _sql_session_last_active,
    escape_like as _escape_like, emit_session_deleted,
)
from hermes_startup_watchdog import report_startup_progress

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

_LAST_ACTIVE_SQL = _sql_session_last_active("s")
_TOKENS_SQL = "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))"
_COST_SQL = "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0)"


def _like(value: str) -> str:
    return f"%{_escape_like(value.lower())}%"


def _cwd_prefix_filter(value: str) -> tuple[list[str], list]:
    from hermes_state_sessions import _cwd_prefix_clause
    clause, params = _cwd_prefix_clause(value)
    return [clause], list(params)


def _one(clause: str, conv=None):
    return lambda v: ([clause], [conv(v) if conv else v])


# Unattended producers only. Not cli/acp/api_server: they also carry interactive conversations
# (the classic CLI, editors, API clients), and this window deletes without the chat-wide opt-in.
SOURCE_RETENTION_SOURCES: tuple[str, ...] = ("cron", "kanban", "subagent", "tool", "recovered")


def _seconds_since(now: float, raw) -> Optional[float]:
    """Age of a state_meta timestamp; None when unset or corrupt (= no prior run)."""
    try:
        return now - float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# Prune/archive filters in evaluation order: (kwarg, applies-when, builder -> (clauses, params)).
# ``applies-when``: "notnone" (numeric/time bounds; 0 is a real bound) or "truthy" ("" = unset).
_PRUNE_FILTERS = (
    # Orphan-swept rows age from the sweep, not old activity, or the next prune deletes them before recovery.
    ("last_active_before", "notnone", lambda v: (
        [_LAST_ACTIVE_SQL + " < ?",
         "(COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)"],
        [v, v])),
    ("last_active_after", "notnone", _one(_LAST_ACTIVE_SQL + " >= ?")),
    ("started_before", "notnone", _one("s.started_at < ?")),
    ("started_after", "notnone", _one("s.started_at >= ?")),
    ("source", "truthy", _one("s.source = ?")),
    ("title_like", "truthy", _one("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'", _like)),
    ("end_reason", "truthy", _one("s.end_reason = ?")),
    ("cwd_prefix", "truthy", _cwd_prefix_filter),
    ("min_messages", "notnone", _one("s.message_count >= ?")),
    ("max_messages", "notnone", _one("s.message_count <= ?")),
    ("model_like", "truthy", _one("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'", _like)),
    ("provider", "truthy", _one("LOWER(COALESCE(s.billing_provider, '')) = ?", str.lower)),
    ("user_id", "truthy", _one("s.user_id = ?")),
    ("chat_id", "truthy", _one("s.chat_id = ?")),
    ("chat_type", "truthy", _one("s.chat_type = ?")),
    ("branch_like", "truthy", _one("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'", _like)),
    ("min_tokens", "notnone", _one(_TOKENS_SQL + " >= ?")),
    ("max_tokens", "notnone", _one(_TOKENS_SQL + " <= ?")),
    ("min_cost", "notnone", _one(_COST_SQL + " >= ?")),
    ("max_cost", "notnone", _one(_COST_SQL + " <= ?")),
    ("min_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) >= ?")),
    ("max_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) <= ?")),
)
_PRUNE_FILTER_NAMES = frozenset(name for name, _, _ in _PRUNE_FILTERS) | {"archived", "include_pinned", "lineage_tips_only"}

# Child ``c`` continues compression-ended ``p`` (same predicate as compression's child lookup).
_CONTINUATION_EDGE_SQL = "p.end_reason = 'compression'\n" + _non_continuation_child_sql("c.", "p.id")


def _continued_ancestors_sql(candidates_where: str) -> str:
    """Compression ancestors of every row *candidates_where* (alias ``s``) does not select."""
    return ("WITH RECURSIVE kept(id) AS ("
            " SELECT p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            f" AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = c.id AND {candidates_where})"
            " UNION"
            " SELECT p.id FROM kept k JOIN sessions c ON c.id = k.id JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            ") SELECT id FROM kept")


# A pin covers the whole conversation, but a store can hold a pinned segment whose later
# continuations were published unpinned; those still belong to the pinned chat.
_PINNED_TAIL_SQL = ("WITH RECURSIVE tail(id) AS ("
                    " SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
                    f" WHERE COALESCE(p.pinned, 0) = 1 AND {_CONTINUATION_EDGE_SQL}"
                    " UNION"
                    " SELECT c.id FROM tail t JOIN sessions p ON p.id = t.id JOIN sessions c ON c.parent_session_id = p.id"
                    f" WHERE {_CONTINUATION_EDGE_SQL}"
                    ") SELECT id FROM tail")


def _not_pinned_sql(alias: str = "s") -> str:
    """Predicate sparing pinned rows and the unpinned continuations a pinned segment covers."""
    return f"COALESCE({alias}.pinned, 0) = 0 AND {alias}.id NOT IN ({_PINNED_TAIL_SQL})"


# One hop up a ``subagent`` row's own compression lineage: its parent is a compressed ``subagent`` segment it
# continues (not a delegate child of that segment, whose ``_delegate_from`` names it).
_SUBAGENT_SEGMENT_PARENT_SQL = ("SELECT p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
                                f" WHERE c.id = ? AND p.source = 'subagent' AND {_CONTINUATION_EDGE_SQL}")
_LINEAGE_WALK_CAP = 1000

# The delegator is ended only when its whole compression lineage is: walk forward along continuation edges and
# look for an open row. A delegator that compressed mid-run (an async parent, or a subagent segment B whose
# delegate G outlives it while B2 runs) still owns its child through its open continuation tip. ``UNION`` dedups
# ids, so a corrupt cycle terminates.
_DELEGATOR_ENDED_SQL = ("WITH RECURSIVE fwd(id) AS ("
                        " SELECT ?"
                        " UNION"
                        " SELECT c.id FROM fwd JOIN sessions p ON p.id = fwd.id JOIN sessions c ON c.parent_session_id = p.id"
                        f" WHERE {_CONTINUATION_EDGE_SQL}"
                        ") SELECT 1 FROM sessions d WHERE d.id = ? AND d.ended_at IS NOT NULL AND NOT EXISTS ("
                        " SELECT 1 FROM fwd JOIN sessions s ON s.id = fwd.id WHERE s.ended_at IS NULL)")


def _delegating_parent_on(conn, session_id: str) -> Optional[str]:
    """The row that delegated *session_id*: the ``parent_session_id`` of the first segment of its compression
    lineage. A compressed child's continuation names its previous segment as parent, not the delegator."""
    current, seen = session_id, {session_id}
    for _ in range(_LINEAGE_WALK_CAP):
        row = conn.execute(_SUBAGENT_SEGMENT_PARENT_SQL, (current,)).fetchone()
        if row is None or row[0] in seen:
            break
        current = row[0]
        seen.add(current)
    row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
    return row[0] if row is not None else None


def _delegator_ended_on(conn, session_id: str) -> bool:
    delegator = _delegating_parent_on(conn, session_id)
    return bool(delegator) and conn.execute(_DELEGATOR_ENDED_SQL, (delegator, delegator)).fetchone() is not None


class SessionMaintenanceMixin:
    """Retention pruning, stale-session archiving and VACUUM policy for SessionDB."""

    def prune_empty_ghost_sessions(self, sessions_dir: Optional[Path] = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400
        def _do(conn):
            ids = [r[0] for r in conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()]
            from hermes_state_mutation_retirement import retire_prunable
            ids = retire_prunable(conn, ids)
            for chunk in _id_chunks(ids):
                conn.execute(f"DELETE FROM sessions WHERE id IN ({_placeholders(chunk)})", chunk)
            if ids:
                self._delete_unreferenced_system_prompts(conn)
            return ids
        removed_ids = self._execute_write(_do) or []
        emit_session_deleted(self, removed_ids, reason="ghost_prune", sessions_dir=sessions_dir)
        from hermes_state_media import collect_retired_media
        collect_retired_media(self)
        for sid in removed_ids if sessions_dir else ():
            self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def _guarded_ids(self, conn, ids: Iterable[str]) -> set:
        """Ids in *ids* protected by a live turn lease / compression lock. Idle compression-ended
        parents are closed, not live, so they are not guarded (prune and delete share this)."""
        return {sid for sid in ids if self._write_guards_reject(conn, sid, allow_closed_compression_parent=True)}

    def _write_guards_reject(self, conn, sid: str, **kwargs) -> bool:
        """True when a live turn lease / compression lock protects ``sid``; expired or
        dead-holder guards are reclaimed and fenced as a side effect."""
        from hermes_state import SessionCompressionInProgressError
        from hermes_state_errors import SessionTurnLeaseLostError
        try:
            self._check_transcript_write_guards(
                conn, sid, compression_lock_holder=None, turn_lease_holder=None,
                reject_active_turn_lease=True, reject_active_compression_lock=True, **kwargs)
        except (SessionCompressionInProgressError, SessionTurnLeaseLostError):
            return True
        return False

    def sweep_orphaned_sessions(
        self, *, max_idle_seconds: float, sources: tuple[str, ...] = ("tui", "desktop", "subagent"),
        exclude_ids: tuple[str, ...] = (), exclude_pinned: bool = False,
        heartbeat_staleness_seconds: Optional[float] = None,
        heartbeat_ownership_grace_seconds: Optional[float] = None, respect_gateway_heartbeats: bool = True,
    ) -> list[str]:
        """Close session rows orphaned by a dead gateway process (its in-process disconnect grace timer died
        with it, leaving ``ended_at IS NULL`` forever).  Rows of ``sources`` whose ``started_at`` AND
        canonical last activity are both older than ``max_idle_seconds`` get
        ``end_reason='startup_orphan_reap'`` (the ``started_at`` predicate protects fresh compression/branch
        children whose copied activity is old).  Only pass sources whose lifecycle the caller owns — never
        messaging platforms like ``telegram`` (ending those triggers a routing loop).  ``exclude_ids`` spares
        rows this process still holds.  Non-destructive: messages kept, row resumable, first-reason-wins.
        With ``respect_gateway_heartbeats`` a row is reaped only when no live backend (heartbeat within
        ``heartbeat_staleness_seconds``, default ``2 * max_idle_seconds``) could own it: B owns S if
        ``B.started_at <= S.started_at + grace`` (default = staleness) — grace covers a migrating backend
        whose sessions predate its first heartbeat, bounded so a PID-reuse respawn cannot protect rows
        forever.  Disable the gate only for state.db-owned sources.  SELECT, live-lease validation and UPDATE
        run in one ``BEGIN IMMEDIATE`` transaction; active leases/locks spare the row, expired guards are
        removed so their owner is fenced.

        See #65194.
        ``exclude_pinned`` is intended for broad automatic sweeps; pinned rows remain explicitly
        recoverable. See #60609.
        """
        srcs = tuple(s for s in sources if s)
        if max_idle_seconds <= 0 or not srcs:
            return []
        hb_staleness, hb_grace = heartbeat_staleness_seconds, heartbeat_ownership_grace_seconds
        if not (hb_staleness and hb_staleness > 0):
            hb_staleness = max_idle_seconds * 2
        if not (hb_grace is not None and hb_grace >= 0):
            hb_grace = hb_staleness
        cutoff = (now := time.time()) - max_idle_seconds
        pin_scope = f" AND {_not_pinned_sql('sessions')}" if exclude_pinned else ""
        orphan_predicate = f"started_at < ? AND {_sql_session_last_active('sessions')} < ?"
        heartbeat_params: tuple[float, ...] = ()
        if respect_gateway_heartbeats:
            orphan_predicate += (" AND NOT EXISTS (SELECT 1 FROM gateway_heartbeats h WHERE"
                                 " h.last_heartbeat >= ? AND h.started_at <= sessions.started_at + ?)")
            heartbeat_params = (now - hb_staleness, hb_grace)
        scope_sql = f" AND source IN ({_placeholders(srcs)}){pin_scope} AND {orphan_predicate}"
        scope_params = (*srcs, cutoff, cutoff, *heartbeat_params)
        def _do(conn):
            rows = conn.execute(f"SELECT id FROM sessions WHERE ended_at IS NULL{scope_sql}",
                                scope_params).fetchall()
            excluded = {str(x) for x in exclude_ids if x}
            victims = [sid for sid in (str(row["id"]) for row in rows)
                       if sid not in excluded and not self._write_guards_reject(conn, sid)]
            if not victims:
                return []
            # Re-apply every predicate under the write lock.
            conn.execute(
                f"UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap'"
                f" WHERE id IN ({_placeholders(victims)}) AND ended_at IS NULL{scope_sql}",
                (time.time(), *victims, *scope_params))
            return victims
        return self._execute_write(_do) or []

    def end_subagents_of_ended_parents(
        self, *, max_idle_seconds: float, exclude_ids: tuple[str, ...] = (),
        end_reason: str = "parent_ended_reap", exclude_pinned: bool = False,
    ) -> list[str]:
        """[daveey] Close open ``subagent`` rows whose delegating parent row is already ended and that have been
        idle (by ``started_at`` AND canonical last activity) at least ``max_idle_seconds``. A delegate child's real
        owner is the process that ran its parent's turn, so ``sweep_orphaned_sessions``' heartbeat guess (any live
        backend started before the row could own it) kept such rows open for as long as some backend outlived them,
        which on a host whose backends restart many times a day is indefinitely. A parent that has ended (reaped or
        closed) has no turn left to own the child; one rotated by compression counts as ended only once its open
        continuation tip has ended too (``_DELEGATOR_ENDED_SQL``). A compressed child's continuation has
        its own compressed segment as ``parent_session_id``, so the delegating parent is found by walking past those
        segments (``_delegating_parent_on``): an open delegator keeps its child's continuation. ``end_reason``
        differs from ``startup_orphan_reap`` so the reap's killed-subagent notice is not re-sent hours late to a chat
        that already received the delegation's own result. Write guards, ``exclude_ids`` and ``exclude_pinned``
        spare rows as in the orphan sweep."""
        if max_idle_seconds <= 0:
            return []
        cutoff = time.time() - max_idle_seconds
        pin_scope = f" AND {_not_pinned_sql('sessions')}" if exclude_pinned else ""
        # Prefilter: the immediate parent is ended (the delegator, or a compressed segment of the child itself).
        scope_sql = (f" AND source = 'subagent'{pin_scope} AND started_at < ?"
                     f" AND {_sql_session_last_active('sessions')} < ?"
                     " AND parent_session_id IS NOT NULL AND EXISTS (SELECT 1 FROM sessions p WHERE"
                     " p.id = sessions.parent_session_id AND p.ended_at IS NOT NULL)")
        scope_params = (cutoff, cutoff)

        def _do(conn):
            rows = conn.execute(f"SELECT id FROM sessions WHERE ended_at IS NULL{scope_sql}", scope_params).fetchall()
            excluded = {str(x) for x in exclude_ids if x}
            victims = [sid for sid in (str(row["id"]) for row in rows)
                       if sid not in excluded and _delegator_ended_on(conn, sid)
                       and not self._write_guards_reject(conn, sid)]
            if not victims:
                return []
            conn.execute(
                f"UPDATE sessions SET ended_at = ?, end_reason = ?"
                f" WHERE id IN ({_placeholders(victims)}) AND ended_at IS NULL{scope_sql}",
                (time.time(), end_reason, *victims, *scope_params))
            return victims
        return self._execute_write(_do) or []

    @staticmethod
    def _prune_filter_where(*, archived: Optional[bool] = None, include_pinned: bool = False,
                            lineage_tips_only: bool = False, include_open: bool = False,
                            **filters) -> tuple[str, list]:
        """Shared WHERE clause for bulk prune/archive selection (alias ``s``): ``_PRUNE_FILTERS``
        AND together, only ended sessions are ever candidates, ``archived`` is tri-state
        (None = both), ``*_like`` are case-insensitive substrings, the rest exact.
        ``lineage_tips_only`` (bulk archive) drops compression ancestors: they are archived with
        their tip, never on their own age — matching an old ancestor would fan out over the lineage
        and hide its OPEN, recently active tip (#115489) — and the canonical Bot Chat (the idle
        sweep's exemption). ``include_open`` lifts the ended guard
        for reversible archive only — never for prune/delete (#139597: desktop sessions never set
        ``ended_at``)."""
        unknown = set(filters) - _PRUNE_FILTER_NAMES
        if unknown:
            raise TypeError("SessionMaintenanceMixin._prune_filter_where() got an unexpected "
                            f"keyword argument {min(unknown)!r}")
        clauses = [] if include_open else ["s.ended_at IS NOT NULL"]
        if lineage_tips_only:
            clauses.append("COALESCE(s.end_reason, '') <> 'compression'")
            # Bulk archive is housekeeping, like the idle sweep: it never retires the canonical
            # Bot Chat (archiving releases its name, #139779). Only a per-chat Archive may. Matched by
            # title alone: compression moves the title onto a VISIBLE continuation tip.
            clauses.append("COALESCE(s.title, '') <> 'Bot Chat'")
        params: list = []
        for name, applies, build in _PRUNE_FILTERS:
            value = filters.get(name)
            if (value is not None) if applies == "notnone" else bool(value):
                new_clauses, new_params = build(value)
                clauses.extend(new_clauses)
                params.extend(new_params)
        if isinstance(archived, bool):
            clauses.append(f"s.archived = {int(archived)}")
        # Pinned is a durable "keep" flag: bulk prune/delete/archive exclude pinned rows unless opted in.
        if not include_pinned:
            clauses.append(_not_pinned_sql())
        return " AND ".join(clauses), params

    def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False) -> tuple[str, list]:
        """Translate the legacy age window into the shared activity filter, then build WHERE.
        ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it
        is unmatched."""
        if (older_than_days is not None and filters.get("last_active_before") is None
                and filters.get("started_before") is None):
            if older_than_days < 0:
                raise ValueError(
                    f"older_than_days must be >= 0, got {older_than_days!r}: a negative "
                    "retention builds a future cutoff that matches every ended session.")
            filters["last_active_before"] = time.time() - (older_than_days * 86400)
        where, params = self._prune_filter_where(source=source, **filters)
        if not whole_lineages:
            return where, params
        # A compressed-away segment ages with its conversation, not on its own: while any later
        # segment stays, deleting it would cut the start off a chat that is still in use.
        return f"{where} AND s.id NOT IN ({_continued_ancestors_sql(where)})", [*params, *params]

    def list_prune_candidates(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                              whole_lineages: bool = False, **filters) -> list[dict[str, Any]]:
        """Dry-run: sessions a matching prune/archive would touch, oldest first (``older_than_days``
        = inactivity threshold: freshest of ``last_activity_at`` / latest message / ``started_at``)."""
        where, params = self._prune_where(older_than_days, source, filters, whole_lineages=whole_lineages)
        return [dict(row) for row in self._read_all(
            f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           {_LAST_ACTIVE_SQL} AS last_active,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY last_active ASC, s.started_at ASC""", params)]

    def count_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                            pinned_only: bool = False, **filters) -> int:
        """Count-only :meth:`list_prune_candidates`; ``pinned_only`` counts rows carrying the pin
        itself, not the continuations it protects (CLI reports spared pinned sessions)."""
        if pinned_only:
            filters["include_pinned"] = True
        where, params = self._prune_where(older_than_days, source, filters)
        if pinned_only:
            where += " AND COALESCE(s.pinned, 0) = 1"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {where}", params)[0])

    def count_open_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None,
                                 **filters) -> int:
        """Count open sessions a matching prune/archive skips (``ended_at`` guard inverted);
        visibility-only. With ``include_open`` the selection already contains them: nothing is skipped."""
        if filters.get("include_open"):
            return 0
        where, params = self._prune_where(older_than_days, source, filters)
        ended_guard = "s.ended_at IS NOT NULL"
        if not where.startswith(ended_guard):
            raise RuntimeError("prune filter lost its ended-session safety guard")
        open_where = f"s.ended_at IS NULL{where[len(ended_guard):]}"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {open_where}", params)[0])

    def archive_stale_sessions(self, idle_days: float, *, exclude_pinned: bool = True) -> int:
        """Archive every session untouched for ``idle_days`` (freshest of ``last_activity_at`` /
        latest message / ``started_at``); may archive unended sessions.  ``archived = 0`` makes
        repeats no-ops; only lineage tips (``end_reason <> 'compression'``) are candidates — a
        stale tip archives its chain via :meth:`set_session_archived`, so an old compressed-away
        root with a recent continuation is never matched.  The hidden canonical Bot Chat (same
        predicate as :meth:`set_session_pinned`) is exempt: only a deliberate archive may retire
        it, since archiving releases its registry title to the next Bot open."""
        if idle_days is None or idle_days < 0:
            return 0
        cutoff = time.time() - float(idle_days) * 86400.0
        pin_clause = f"AND {_not_pinned_sql()}" if exclude_pinned else ""
        rows = self._read_all(
            f"""
            SELECT s.id FROM sessions s
            WHERE s.archived = 0
              AND COALESCE(s.end_reason, '') <> 'compression'
              {pin_clause}
              AND NOT (COALESCE(s.hidden, 0) <> 0 AND COALESCE(s.title, '') = ?)
              AND {_LAST_ACTIVE_SQL} < ?
            ORDER BY s.started_at ASC
            """, (self.CANONICAL_BOT_CHAT_TITLE, cutoff))
        for row in rows:
            # Sweep provenance: a later compression/resume of this lineage un-hides it (#117713).
            self._auto_archive_lineage(row[0])
        return len(rows)

    def maybe_auto_archive(
        self, idle_days: float = 3, min_interval_hours: int = 24, exclude_pinned: bool = True,
    ) -> dict[str, Any]:
        """Idempotent, non-destructive auto-archive of sessions idle for ``idle_days``; state_meta
        ``last_auto_archive`` gates runs within ``min_interval_hours``. Never raises."""
        result: dict[str, Any] = {"skipped": False, "archived": 0}
        try:
            now = time.time()
            try:
                last = float(self.get_meta("last_auto_archive") or 0.0)
            except (TypeError, ValueError):
                last = 0.0  # corrupt meta; treat as no prior run
            if last and now - last < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            # Startup-watchdog lease: the archive sweep is I/O-bound (near-zero CPU),
            # which the watchdog's CPU fallback misreads as a parked deadlock.
            # No-op when the watchdog is not armed; never raises.
            report_startup_progress(900.0, phase="state_db_auto_archive")
            archived = result["archived"] = self.archive_stale_sessions(idle_days, exclude_pinned=exclude_pinned)
            # Record even a zero-archive run so we don't re-sweep every call.
            self.set_meta("last_auto_archive", str(now))
            if archived > 0:
                logger.info(
                    "state.db auto-archive: archived %d session(s) idle >= %s days", archived, idle_days,
                )
        except Exception as exc:
            logger.warning("state.db auto-archive failed: %s", exc)
            result["error"] = str(exc)
        return result
    # [daveey] Session-outcome contract, "Drafts": a chat opened and never used is not a sidebar row to resolve.
    DRAFT_SOURCES = ("desktop", "tui", "cli")
    DRAFT_MAX_AGE_S = 86400.0
    DRAFT_SWEEP_INTERVAL_S = 3600.0

    def empty_draft_ids(self, *, older_than_s: float = DRAFT_MAX_AGE_S) -> list:
        """Ids of interactive rows that never got a message and were last touched ``older_than_s`` ago
        (read-only, so a read-only handle can ask before anyone opens the store writable). Pinned rows,
        the hidden canonical Bot Chat and compression continuations are exempt."""
        cutoff = time.time() - float(older_than_s)
        return [r[0] for r in self._read_all(
            f"""
            SELECT s.id FROM sessions s
            WHERE s.archived = 0
              AND COALESCE(s.message_count, 0) = 0
              AND s.source IN ({_placeholders(self.DRAFT_SOURCES)})
              AND COALESCE(s.end_reason, '') <> 'compression'
              AND {_not_pinned_sql()}
              AND NOT (COALESCE(s.hidden, 0) <> 0 AND COALESCE(s.title, '') = ?)
              AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM sessions p WHERE p.id = s.parent_session_id
                                AND p.end_reason = 'compression')
              AND {_LAST_ACTIVE_SQL} < ?
            """, (*self.DRAFT_SOURCES, self.CANONICAL_BOT_CHAT_TITLE, cutoff))]

    def archive_empty_drafts(self, *, older_than_s: float = DRAFT_MAX_AGE_S) -> int:
        """Archive (soft-hide, sweep provenance) the :meth:`empty_draft_ids` rows. Returns rows archived."""
        rows = self.empty_draft_ids(older_than_s=older_than_s)
        for row in rows:
            self._auto_archive_lineage(row)
        if rows:
            logger.info("state.db: archived %d empty draft session(s) older than %.0fh", len(rows),
                        older_than_s / 3600)
        return len(rows)

    def maybe_archive_empty_drafts(self) -> int:
        """:meth:`archive_empty_drafts` at most once per ``DRAFT_SWEEP_INTERVAL_S`` after a sweep
        that archived rows (state_meta ``last_draft_archive``; an empty sweep is read-only). Runs whatever ``sessions.auto_archive`` says. Never raises."""
        try:
            now = time.time()
            since = _seconds_since(now, self.get_meta("last_draft_archive"))
            if since is not None and since < self.DRAFT_SWEEP_INTERVAL_S:
                return 0
            count = self.archive_empty_drafts()
            if count:  # a sweep that found nothing writes nothing: GET /api/sessions polls reach here
                self.set_meta("last_draft_archive", str(now))
            return count
        except Exception:  # health: allow BLE001 -- a maintenance chore must never break its tick; logged
            logger.warning("state.db empty-draft archive failed", exc_info=True)
            return 0

    def prune_sessions(self, older_than_days: Optional[float] = 90, source: str | None = None,
                       sessions_dir: Optional[Path] = None, exclude_active_write_guards: bool = False,
                       deleted_ids: Optional[list] = None, **filters) -> int:
        """Delete ended sessions inactive for ``older_than_days`` (an explicit ``started_before`` /
        ``last_active_before`` overrides it; None = no implicit bound) matching the filters.
        Children outside the window are orphaned (parent NULLed), not cascade-deleted.  With
        *sessions_dir*, transcript files are removed outside the DB transaction.
        ``exclude_active_write_guards`` (automatic maintenance) skips rows under a live turn lease
        or compression lock while expired/dead holders are reclaimed and fenced.  A compression
        ancestor is deleted only together with every continuation after it (``whole_lineages``).
        ``deleted_ids`` (the owning gateway) receives the removed ids, so it can retire their
        in-memory routes and live sessions after the commit."""
        if filters.get("include_open"):
            # Deletion is not reversible, so the ended-session guard is structural here: the
            # archive-only escape hatch (#139597) must never reach the delete path.
            raise ValueError("prune_sessions() never deletes open sessions: include_open is archive-only")
        where, where_params = self._prune_where(older_than_days, source, filters, whole_lineages=True)
        removed_ids: list[str] = []
        def _do(conn):
            cursor = conn.execute(f"SELECT s.id FROM sessions s WHERE {where}", where_params)
            session_ids = {row["id"] for row in cursor.fetchall()}
            if exclude_active_write_guards:
                session_ids -= self._guarded_ids(conn, session_ids)
            from hermes_state_mutation_retirement import prune_retired_tombstones, retire_prunable
            session_ids = retire_prunable(conn, sorted(session_ids))
            prune_retired_tombstones(conn)  # every prune pass also reclaims expired deletion tombstones
            if not session_ids:
                return 0
            # Batched: a cron-heavy store prunes tens of thousands of ids in one call.
            for chunk in _id_chunks(session_ids):
                ph = _placeholders(chunk)
                conn.execute(f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", chunk)
                removed_ids.extend(chunk)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)
        count = self._execute_write(_do)
        if count:
            emit_session_deleted(self, removed_ids, reason="prune", sessions_dir=sessions_dir)
        from hermes_state_media import collect_retired_media
        collect_retired_media(self)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        if deleted_ids is not None:
            deleted_ids.extend(removed_ids)
        return count

    def _page_pragmas(self, names: tuple[str, ...], fail_msg: str) -> Optional[list]:
        """Integer PRAGMAs over the existing connection (never a byte probe); None + debug log on failure."""
        try:
            with self._read_ctx() as conn:
                if self._conn is None:
                    return None
                return [int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in names]
        except Exception as exc:
            logger.debug(fail_msg, exc)
            return None

    def logical_size_bytes(self) -> Optional[int]:
        """``page_count * page_size``: main-file size once the WAL is checkpointed in.  Prefer
        over ``os.path.getsize`` when reporting a VACUUM: in WAL mode the rewrite lands in
        ``-wal`` and the checkpoint is refused while another connection holds a read-mark, so
        a stat() delta understates the win and can go negative."""
        values = self._page_pragmas(("page_count", "page_size"), "Could not read logical DB size: %s")
        return None if values is None else values[0] * values[1]

    def _freelist_ratio(self) -> Optional[float]:
        """Reclaimable fraction (``freelist_count / page_count``) gating VACUUM in
        :meth:`maybe_auto_prune_and_vacuum`; None = fall back to the time throttle.

        ``PRAGMA freelist_count / PRAGMA page_count`` read over the existing connection (never a byte-level
        probe of the live file — see ``sqlite_safe_read``). This is what VACUUM would actually give back; it
        is the gate :meth:`maybe_auto_prune_and_vacuum` uses to decide whether a full rewrite pays off
        (#54189).
        """
        values = self._page_pragmas(("page_count", "freelist_count"), "Could not read freelist ratio: %s")
        return None if values is None else (values[1] / values[0] if values[0] > 0 else 0.0)

    def _try_checkpoint(self, mode: str, fail_msg: str) -> None:
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception as exc:
            logger.debug(fail_msg, exc)

    def vacuum(self) -> int:
        """VACUUM to reclaim space after large deletes (SQLite never shrinks on its own).
        Takes an exclusive lock — callers must ensure no other writers are active.  FTS5
        segments are merged first (:meth:`optimize_fts`) so their pages are reclaimed too;
        returns the number of FTS indexes optimized (0 on merge failure / no FTS). A quarantined
        handle (corrupt image, replaced file, lost WAL generation) raises before any rewrite: a
        VACUUM reads every page and commits the result back, turning contained damage into an
        amplified one (#105670). Same guard ``_execute_write`` applies to every write."""
        self._raise_if_db_corrupt()
        optimized = 0
        from hermes_state_fts_online import FtsFreeRefused

        try:
            optimized = self.optimize_fts()  # manages its own lock
        except FtsFreeRefused:
            raise  # the gate saw damage it will not let a page-freeing rewrite touch: no VACUUM either
        except Exception as exc:
            logger.warning("FTS optimize before VACUUM failed: %s", exc)
        with self._lock:
            self._raise_if_db_replaced()
            if self._conn is None:
                self._reopen_after_close_locked(context="write")
            # PASSIVE, not TRUNCATE: a manual `hermes sessions vacuum` runs in a transient CLI
            # process; a TRUNCATE reset here would race a live gateway writer.
            self._try_checkpoint("PASSIVE", "WAL checkpoint (PASSIVE) before VACUUM failed: %s")
            self._conn.execute("VACUUM")
            # VACUUM rewrites every page THROUGH the WAL; without this TRUNCATE a 3 GB DB leaves a 3 GB -wal.
            self._try_checkpoint("TRUNCATE", "WAL checkpoint (TRUNCATE) after VACUUM failed: %s")
            # TRUNCATE may replace the WAL inode; adopt the new sidecars so the
            # write-path generation guard does not halt this connection.
            self._record_db_file_identity()
        return optimized

    def maybe_prune_source_retention(
        self, source_days: Optional[dict], min_interval_hours: int = 24, sessions_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Per-source retention (``sessions.source_retention_days``), independent of ``auto_prune``: each
        ``{source: days}`` entry prunes that source's ENDED sessions inactive for ``days`` and closes its
        stale open rows (they then get a full window), at most once per ``min_interval_hours`` (own
        throttle key). Only the unattended automation sources in :data:`SOURCE_RETENTION_SOURCES` are
        eligible: a chat source (desktop, tui, cli, acp, api_server, slack...) or a days value that is not a whole number >= 1
        is skipped with a warning, so a typo never deletes conversations. Pinned and mid-turn rows are
        spared as in :meth:`maybe_auto_prune_and_vacuum`. No VACUUM: freed pages are reused by later
        writes, which keeps the file bounded without a full rewrite. Never raises; returns
        ``{"skipped", "pruned": {source: n}, "closed": {source: n}}`` plus ``"error"`` on failure."""
        from hermes_state_repair import _release_auto_maintenance_lock, _try_acquire_auto_maintenance_lock
        result: dict[str, Any] = {"skipped": False, "pruned": {}, "closed": {}}
        plan = self._source_retention_plan(source_days)
        if not plan:
            result["skipped"] = True
            return result
        maintenance_lock = _try_acquire_auto_maintenance_lock(self.db_path)
        if maintenance_lock is None:
            result["skipped"] = True
            return result
        try:
            now = time.time()
            since = _seconds_since(now, self.get_meta("last_source_retention_prune"))
            if since is not None and since < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            for source, days in plan:
                report_startup_progress(900.0, phase="state_db_source_retention")
                result["pruned"][source] = self.prune_sessions(
                    older_than_days=days, source=source, sessions_dir=sessions_dir,
                    exclude_active_write_guards=True)
                result["closed"][source] = len(self.sweep_orphaned_sessions(
                    max_idle_seconds=float(days) * 86400.0, sources=(source,), exclude_pinned=True,
                    respect_gateway_heartbeats=False))
            self.set_meta("last_source_retention_prune", str(now))
            if any(result["pruned"].values()) or any(result["closed"].values()):
                logger.info("state.db source retention: pruned %s, closed %s", result["pruned"], result["closed"])
        except Exception as exc:
            logger.warning("state.db source retention failed: %s", exc, exc_info=True)
            result["error"] = str(exc)
        finally:
            _release_auto_maintenance_lock(maintenance_lock)
        return result

    def _source_retention_plan(self, source_days: Optional[dict]) -> list[tuple[str, int]]:
        """Valid ``(source, days)`` pairs of ``sessions.source_retention_days``, warning once per bad entry."""
        if not isinstance(source_days, dict):
            if source_days:
                logger.warning("sessions.source_retention_days must be a mapping of source: days, got %r",
                               source_days)
            return []
        plan = []
        for source, days in source_days.items():
            if source not in SOURCE_RETENTION_SOURCES:
                logger.warning("sessions.source_retention_days: %r skipped; only automation sources (%s) "
                               "can have their own retention", source, ", ".join(SOURCE_RETENTION_SOURCES))
            elif isinstance(days, bool) or not isinstance(days, int) or days < 1:
                logger.warning("sessions.source_retention_days.%s: %r skipped; use a whole number of days >= 1",
                               source, days)
            else:
                plan.append((source, days))
        return plan

    def maybe_auto_prune_and_vacuum(
        self, retention_days: int = 90, min_interval_hours: int = 24, vacuum: bool = True,
        sessions_dir: Optional[Path] = None, min_vacuum_interval_days: int = 30,
        min_vacuum_freelist_ratio: float = AUTO_VACUUM_MIN_FREELIST_RATIO,
    ) -> dict[str, Any]:
        """Idempotent startup auto-maintenance (never raises): prune inactive sessions, reap stale open
        state-owned rows, optional VACUUM.  Runs at most once per ``min_interval_hours``; VACUUM has its own
        ``min_vacuum_interval_days`` throttle and also requires ``freelist_count / page_count`` >
        ``min_vacuum_freelist_ratio`` so a small prune on a dense multi-GB database never triggers a full
        rewrite.  Stale-open reconciliation: cron/kanban/subagent/one-shot CLI rows never set ``ended_at``
        when their process dies and prune only deletes ended rows, so after pruning, open rows from
        :attr:`_AUTO_PRUNE_STALE_OPEN_SOURCES` older than ``retention_days`` are closed
        (``startup_orphan_reap``); they stay resumable and age from their close.  Returns ``{"skipped",
        "pruned", "closed", "vacuumed"}`` plus ``"freelist_ratio"`` when a VACUUM was considered and
        ``"error"`` on failure.

        Records the last run timestamp in state_meta so subsequent calls within ``min_interval_hours``
        no-op. Designed to be called once at startup from long-lived entrypoints (CLI, gateway, cron
        scheduler). See #54189.
        When *sessions_dir* is provided, on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for pruned sessions are removed as part of the same sweep (issue #3015).
        Messaging and UI sources are never touched here. See #54189.
        """
        from hermes_state_repair import _release_auto_maintenance_lock, _try_acquire_auto_maintenance_lock
        result: dict[str, Any] = {"skipped": False, "pruned": 0, "closed": 0, "vacuumed": False}
        if retention_days is None or retention_days < 0:
            # A negative retention would build a future cutoff and match every ended
            # session; auto_prune=false is the disable switch, not a negative bound.
            logger.warning(
                "state.db auto-maintenance skipped: sessions.retention_days=%r is outside the allowed "
                "range (a whole number of days >= 0); set sessions.auto_prune: false to disable pruning",
                retention_days)
            result["skipped"] = True
            return result
        maintenance_lock = _try_acquire_auto_maintenance_lock(self.db_path)
        if maintenance_lock is None:
            result["skipped"] = True
            return result
        try:
            now = time.time()
            since_prune = _seconds_since(now, self.get_meta("last_auto_prune"))
            if since_prune is not None and since_prune < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            # Prune first: orphans closed below get a full retention window.
            # Startup-watchdog leases: each long step is I/O-bound (near-zero CPU), which the
            # watchdog's CPU fallback misreads as a parked deadlock. Leases are clamped to
            # _MAX_LEASE_S=900 per call, so a multi-minute step renews per step rather than
            # once at entry. No-op when the watchdog is not armed; never raises.
            report_startup_progress(900.0, phase="state_db_auto_prune")
            result["pruned"] = pruned = self.prune_sessions(
                older_than_days=retention_days, sessions_dir=sessions_dir, exclude_active_write_guards=True)
            report_startup_progress(900.0, phase="state_db_auto_sweep")
            closed = self.sweep_orphaned_sessions(
                max_idle_seconds=float(retention_days) * 86400.0,
                sources=self._AUTO_PRUNE_STALE_OPEN_SOURCES, exclude_pinned=True,
                respect_gateway_heartbeats=False,  # state-owned lifecycles, not gateway heartbeats
            )
            result["closed"] = len(closed)
            # VACUUM only if rows were freed, the time throttle passed AND the
            # freelist ratio passed — it holds an exclusive lock for a full rewrite.
            since_vacuum = _seconds_since(now, self.get_meta("last_vacuum"))
            vacuum_due = since_vacuum is None or since_vacuum >= min_vacuum_interval_days * 86400
            if vacuum and pruned > 0 and vacuum_due:
                result["freelist_ratio"] = ratio = self._freelist_ratio()
                # Same admission `hermes sessions optimize` runs: VACUUM plus the TRUNCATE checkpoint
                # retire the WAL generation a sibling writer (gateway, Desktop, dashboard, cron) still
                # holds, and that is exactly the state every agent then refuses turns in (#110054).
                # The foreign scan skips our own pid, so it is paired with the in-process arm: under
                # one multiplexed process another live SessionDB generation for this same path is
                # just as much a holder as another process would be.
                # Automatic maintenance only ever SKIPS — a turn is never refused over housekeeping.
                from hermes_state_holders import (
                    foreign_state_db_holders, in_process_state_db_holders)
                holders = (foreign_state_db_holders(self.db_path)
                           + in_process_state_db_holders(self.db_path, exclude=self))
                if holders:
                    result["vacuum_skipped_holders"] = len(holders)
                    logger.debug(
                        "state.db auto-maintenance: skipping VACUUM, %d other process(es) hold the "
                        "store or a WAL sidecar (%s)",
                        len(holders), ", ".join(f"{pid}:{target}" for pid, target in holders[:3]))
                elif ratio is None or ratio > min_vacuum_freelist_ratio:
                    try:
                        # VACUUM rewrites every page with ~zero CPU: renew the lease here so
                        # a multi-minute rewrite on a large state.db never outlives the clamp.
                        report_startup_progress(900.0, phase="state_db_auto_vacuum")
                        self.vacuum()
                        result["vacuumed"] = True
                        self.set_meta("last_vacuum", str(now))
                    except Exception as exc:
                        logger.warning("state.db VACUUM failed: %s", exc)
                else:
                    logger.debug("state.db auto-maintenance: skipping VACUUM, only "
                                 "%.1f%% of pages reclaimable (threshold %.0f%%)",
                                 ratio * 100.0, min_vacuum_freelist_ratio * 100.0)
            # Record even when pruned == 0 so the throttle holds.
            self.set_meta("last_auto_prune", str(now))
            if closed or pruned > 0:
                logger.info("state.db auto-maintenance: closed %d stale open session(s), "
                            "pruned %d session(s) inactive for %d days%s",
                            len(closed), pruned, retention_days, " + VACUUM" if result["vacuumed"] else "")
        except Exception as exc:
            # Maintenance must never block startup.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)
        finally:
            _release_auto_maintenance_lock(maintenance_lock)
        return result
