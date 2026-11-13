"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _auto_decompose_min_age_seconds, _board_slugs, _positive_int_setting, logger,
)
from hermes_cli import loop_accounting as _la


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch

_UNSET = object()

_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass
class _DispatcherSettings:
    """``kanban.*`` dispatch settings, read once at boot (restart to apply).

    Exception: ``max_in_progress`` is re-read every tick (``_KanbanDispatcher.live_max_in_progress``);
    this field is only its fallback until a tick has run."""

    interval: float
    max_spawn: Any
    max_in_progress: Optional[int]
    failure_limit: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]


def _resolve_dispatcher_settings(kanban_cfg: dict, kb: Any) -> _DispatcherSettings:
    """Parse and log the dispatcher settings in their established order."""
    try:
        interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (ValueError, TypeError):
        logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                       kanban_cfg.get("dispatch_interval_seconds"))
        interval = 60.0
    interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

    max_spawn = kanban_cfg.get("max_spawn")
    if max_spawn is not None:
        logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress")
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile"),
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings) -> None:
        self.kb = kb
        self.settings = settings
        self.successful_scopes: list = []
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}
        self._rr_offset = 0  # round-robin start board for spawning ticks (_fair_board_order)
        self._logged_max_in_progress: Any = _UNSET  # last host cap a tick used (live_max_in_progress)

    def _board_slugs(self) -> list:
        return _board_slugs(self.kb)

    def live_max_in_progress(self) -> Optional[int]:
        """``kanban.max_in_progress`` re-read for this tick (config load is mtime-cached). [daveey]

        The host cap is the one dispatch setting an operator lowers to contain load, and the
        dashboard and ``hermes kanban daemon`` already read it live. Freezing it at boot let a
        hub gateway booted at 16 keep claiming to 16/16 for an hour after config said 7, while
        the dashboard showed 15/7. An explicit positive value wins; an unset, invalid or
        unreadable key keeps the cap the previous tick used (the boot value on the first tick),
        never the boot value: boot 16 -> config 7 -> one bad read must not reopen 16. Raising the
        cap takes an explicit value; restoring the memory-derived default takes a restart.
        """
        configured = _kbd().configured_max_in_progress()
        if configured is not None:
            effective = configured
        elif self._logged_max_in_progress is not _UNSET:
            effective = self._logged_max_in_progress
        else:
            effective = self.settings.max_in_progress
        if effective != self._logged_max_in_progress:
            if self._logged_max_in_progress is not _UNSET:
                logger.info("kanban dispatcher: max_in_progress=%s (was %s; config re-read live)",
                            effective, self._logged_max_in_progress)
            self._logged_max_in_progress = effective
        return effective

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        from hermes_cli import kanban_db as _kb
        with _kb.pin_first_board_resolution():
            path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True

    def tick_once_for_board(self, slug: str, *, suppress_spawn: bool = False,
                            spawn_quota: Optional[int] = None) -> Optional[object]:
        """Run one dispatch_once for a specific board.

        The per-board DB is opened explicitly so boards never share a
        connection or claim across each other. ``suppress_spawn`` (core-update
        drain hold) runs reclaim, wake probes and promotion but claims nothing.
        ``spawn_quota`` caps this board's spawns at its cross-board share of the host slots (tick_once).
        """
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}
        kwargs["max_in_progress"] = self.live_max_in_progress()
        if spawn_quota is not None:
            kwargs["spawn_quota"] = spawn_quota
        if suppress_spawn:
            kwargs["max_spawn"] = 0  # budget gate refuses every claim; bookkeeping phases still run
            kwargs["observation_update_drain"] = True  # diagnostic only, never an admission input
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            # Pin-first: the tick is machine flow — on a box whose env pins
            # HERMES_KANBAN_DB every enumerated slug must resolve to the pinned
            # file, or the dispatcher reads per-slug DBs nobody writes.
            from hermes_cli import kanban_db as _kb
            with _kb.pin_first_board_resolution():
                conn = _kbc().connect(board=slug)
                profiles, lanes = self._receipt_scope()
                result = _kbd().dispatch_once(conn, board=slug, **kwargs)
                if not result.skipped_locked and result.scheduling_admitted:
                    after_profiles, after_lanes = self._receipt_scope()
                    path = conn.execute("PRAGMA database_list").fetchone()[2]
                    if path:
                        self.successful_scopes.append((
                            str(Path(path).resolve()), sorted(profiles & after_profiles),
                            sorted(lanes & after_lanes), time.monotonic()))
                return result
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                self._start_auto_recovery(slug, fingerprint[0])
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    @staticmethod
    def _start_auto_recovery(slug: str, db_path: str) -> None:
        """A board with table b-tree damage stays down until it is rebuilt: start the rebuild now
        (``hermes_cli.kanban_db_recover``) instead of waiting for someone to notice. It runs in a
        detached child (this process may itself be stranded on deleted sidecars, and its raw reads
        would cancel its own locks), re-checks the damage from a clean process, holds the board's
        recovery lock, and writes the board back in place, which changes the fingerprint and
        lifts this pause. Rate-limited per board; ``kanban.auto_recover_corruption: false`` opts out."""
        try:
            from hermes_cli import kanban_db_recover as rec
            if db_path and rec.auto_recover_enabled():
                rec.spawn_recovery(Path(db_path))
        except Exception:  # health: allow BLE001 -- the pause and its ERROR log above still stand
            logger.exception("kanban dispatcher: could not start automatic recovery of board %s", slug)

    def _fair_board_order(self) -> list:
        """Boards for this tick, rotated one place per tick (round-robin start). [daveey]

        ``kanban.max_in_progress`` is a HOST-wide budget that boards draw on in
        tick order. A fixed order (``default`` first, then alphabetical) let a
        standing backlog on ``default`` take every slot freed after a core-update
        drain hold or a worker exit, so ready cards on other boards waited behind
        it indefinitely. Rotating the start gives each board first pick in turn
        when slots are plentiful; under contention ``tick_once`` ranks cards across boards instead.
        """
        slugs = list(self._board_slugs())
        if len(slugs) < 2:
            return slugs
        start = self._rr_offset % len(slugs)
        self._rr_offset += 1
        return slugs[start:] + slugs[:start]

    @_la.accounted("dispatcher.tick")
    def tick_once(self, *, suppress_spawn: bool = False) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs.

        When free host slots are fewer than the ready cards on all boards, each board gets a spawn quota from one
        cross-board ranking (``hermes_cli.kanban_board_share``: priority order plus a starvation guard). [daveey]
        """
        self.successful_scopes = []
        from hermes_cli import kanban_board_share as share
        with share.tick_counts():
            if not suppress_spawn:
                order = self._fair_board_order()
                quotas = share.shares(order, self.live_max_in_progress())
                if quotas is not None:
                    return share.run(order, quotas, self._board_pass)
                return [(slug, self._board_pass(slug)) for slug in order]
            return [(slug, self._board_pass(slug, suppress_spawn=True)) for slug in self._board_slugs()]

    def _board_pass(self, slug: str, **kwargs: Any) -> Optional[object]:
        """``tick_once_for_board`` as one ``dispatcher.board/<slug>`` pass (``hermes_cli.loop_accounting``): a
        quarantined or lock-skipped board counts as a skip. [daveey]"""
        with _la.pass_(f"dispatcher.board/{slug}") as acct:
            result = self.tick_once_for_board(slug, **kwargs)
            if result is None or getattr(result, "skipped_locked", False):
                acct.skip()
            return result

    @staticmethod
    def _receipt_scope() -> tuple[set, set]:
        """Only existing profiles admitted by native dispatch policy."""
        from hermes_cli.profiles import list_profile_names
        predicate = _kbd()._profile_exists_fn()
        if predicate is None:
            return set(), set()
        # Names only: ``list_profiles`` also probes each profile's gateway liveness and config, twice per board
        # per tick (7.6% of hub gateway CPU at 12 boards, 2026-11-12). [daveey]
        profiles = {name for name in list_profile_names() if predicate(name)}
        lanes = {"ready"}
        if _kbd().review_dispatch_enabled():
            lanes.add("review")
        return profiles, lanes

    @contextlib.contextmanager
    def _unowned_board_work(self, slug):
        """Hold the same canonical lock for pre-dispatch work and owner changes."""
        from hermes_cli.kanban_dispatch_owner import connection_db_path, get_dispatch_owner
        with contextlib.ExitStack() as stack:
            allowed = None
            try:
                conn = stack.enter_context(_kbc().connect_closing(board=slug))
                path = connection_db_path(conn, validate=False)
                held = stack.enter_context(_kbc()._dispatch_tick_lock(path))
                if held:
                    connection_db_path(conn)
                    if get_dispatch_owner(path) is None:
                        allowed = conn
            except (OSError, ValueError, UnicodeError, RecursionError, sqlite3.Error,
                    _kbc().KanbanDbCorruptError) as exc:
                # tick_once owns corruption quarantine and its actionable log;
                # pre-work must not emit a traceback on every probe.
                logger.debug("kanban dispatcher: pre-dispatch work denied on %s (%s)", slug, exc)
            yield allowed

    def ready_nonempty(self) -> bool:
        """Is there a ready+assigned+unclaimed task on ANY board the dispatcher would spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        from hermes_cli import kanban_db as _kb
        with _kb.pin_first_board_resolution():
            for slug in self._board_slugs():
                try:
                    with self._unowned_board_work(slug) as conn:
                        if conn is not None and (kbd.has_spawnable_ready(conn) or (
                                _review_probe and kbd.has_spawnable_review(conn))):
                            return True
                except Exception:
                    # A corrupt DB or failed probe must not hide healthy peers.
                    logger.exception("kanban dispatcher: health probe failed on board %s", slug)
        return False

    @_la.accounted("dispatcher.auto_decompose", rows=True)
    def auto_decompose_tick(self, auto_decompose_per_tick: int, min_age_seconds: Optional[int] = None) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Triage cards younger than
        ``min_age_seconds`` (default: ``kanban.auto_decompose_min_age_seconds``)
        wait for a later tick. Returns the number decomposed.
        """
        if min_age_seconds is None:
            min_age_seconds = _auto_decompose_min_age_seconds()
        self._ad_min_age = max(int(min_age_seconds), 0)
        try:
            from hermes_cli import kanban_decompose as _decomp

        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        from hermes_cli import kanban_db as _kb
        with _default_profile_secret_scope(), _kb.pin_first_board_resolution():
            for slug in self._board_slugs():
                if attempted >= auto_decompose_per_tick:
                    break

                # The decomposer connects without a board kwarg. Keep selection
                # local to this tick: a process-env pin makes concurrent discovery
                # incorrectly offer environments:[kanban] skills.
                try:
                    with _kb.scoped_current_board(slug), self._unowned_board_work(slug) as conn:
                        if conn is not None:
                            used, done = self._decompose_board(_decomp, slug, auto_decompose_per_tick - attempted)
                            attempted += used
                            successes += done
                except Exception:  # health: allow BLE001 -- per-board isolation boundary logs traceback; context manager restores board
                    # Contain connection, callback and cleanup failures per board;
                    # the caller must still reach tick_once for healthy peers.
                    logger.exception("kanban auto-decompose: board %s failed", slug)

        return successes

    def _decompose_board(self, decomp, slug, budget):
        try:
            triage_ids = decomp.list_triage_ids(
                skip_awaiting_human=True, min_age_seconds=getattr(self, "_ad_min_age", 0))
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            logger.exception("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
            return 0, 0
        attempted = successes = 0
        for tid in triage_ids:
            if attempted >= budget:
                break
            attempted += 1
            successes += self._decompose_one(decomp, slug, tid)
        return attempted, successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        # The autonomous agent/auxiliary call starts here: recheck the claim-quiet lease immediately before it
        # and skip only this attempt (the triage card stays as it was). Read-only; uncertain reads are open.
        try:
            from agent.claim_quiet import read_active
            lease = read_active()
        except Exception:  # health: allow BLE001 -- a failed quiet read must fail open, never block decomposition
            lease = None
        if lease is not None:
            logger.info("kanban auto-decompose [%s]: %s skipped: claim-quiet episode %s (deferred to a later tick)",
                        slug, tid, lease.episode_id)
            return 0
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


@contextlib.contextmanager
def _default_profile_secret_scope():
    """Install the gateway launch profile's secret scope while multiplexing is on.

    The tick runs via ``_to_thread_process_service`` in a fresh context, so no
    per-turn scope exists and ``get_secret`` fails closed. The decomposer's aux
    LLM reads ``auxiliary.*`` from ``get_hermes_home()``, so its credentials come
    from that same home. No-op for single-profile gateways.
    """
    from agent.secret_scope import is_multiplex_active, reset_secret_scope, set_secret_scope
    from hermes_constants import get_hermes_home
    from tui_gateway.launch_profile_policy import owner_secret_scope

    if not is_multiplex_active():
        yield
        return
    token = set_secret_scope(owner_secret_scope(Path(get_hermes_home())), profile_home=str(get_hermes_home()))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is not None and getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned


def _external_drain_hold(observed_at: float) -> tuple[str, str]:
    """``(principal, since HH:MM:SS)`` of the external drain (``.drain_request.json``) holding dispatch.

    Read through ``gateway.drain_control`` only to name the hold in the log; the gate itself is the runner's
    ``_external_drain_active`` flag, so an unreadable marker degrades the line, never the hold. ``since`` is
    the marker's ``requested_at`` in local time, else *observed_at* (when the dispatcher first saw it). [daveey]"""
    from datetime import datetime

    from gateway.drain_control import read_drain_request
    from gateway.memory_status import _parse_iso

    body = read_drain_request() or {}  # never raises: absent -> None, garbled -> {}
    principal = str(body.get("principal") or "unknown principal")
    since = _parse_iso(body.get("requested_at"))  # None when missing or unparseable
    when = since.astimezone() if since is not None else datetime.fromtimestamp(observed_at)
    return principal, when.strftime("%H:%M:%S")
