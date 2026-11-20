"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Per-tick work lives in
``kanban_watchers_notifier`` / ``kanban_watchers_dispatcher``; shared plumbing
in ``kanban_watchers_common``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _kanban_dispatch_allowed,
    _kanban_dispatch_gate,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _gc_retention_days,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _external_drain_hold,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_GC_INTERVAL_SECONDS = 3600.0
_HEALTH_WINDOW = 6


async def _await_process_service(func, /, *args, into=None):
    """Offload process-service work; cancellation still observes the worker.

    ``asyncio.to_thread`` does not stop the thread. A cancelled waiter waits for
    it so a late return value can be closed. Nested cancels are absorbed until
    the worker finishes. Process-service context is unchanged.
    """
    worker = asyncio.ensure_future(_to_thread_process_service(func, *args))
    try:
        result = await asyncio.shield(worker)
        if into is not None:
            into.append(result)
        return result
    except asyncio.CancelledError:
        while not worker.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({worker})
        if into is not None and not worker.cancelled() and worker.exception() is None:
            into.append(worker.result())
        raise


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        health = getattr(self, "_kanban_dispatcher_health", None)
        if health is not None:
            health.close()
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        lease = getattr(self, "_kanban_dispatcher_lease", None)
        if lease is not None and lease.handle is handle:
            lease.release()
        else:
            _release_singleton_lock(handle)

    def _kanban_singleton_lease(self, lock_path: Path, name: str):
        """A retrying, root-preferring lease on a host-wide singleton lock (gateway/kanban_singleton.py)."""
        from gateway import kanban_singleton as _ks
        return _ks.SingletonLease(lock_path, name=name, retry_seconds=_ks.DEFAULT_RETRY_SECONDS,
                                  grace_seconds=_ks.DEFAULT_GRACE_SECONDS,
                                  warn_after_seconds=_ks.DEFAULT_WARN_AFTER_SECONDS)

    async def _acquire_kanban_lease(self, lease) -> bool:
        """Retry *lease* every ``lease.retry_interval()`` until held; False when the gateway stops first."""
        while self._running:
            if lease.try_acquire():
                return True
            await self._sleep_between_ticks(lease.retry_interval())
        return False

    _KANBAN_WATCHER_START_DELAY = 5.0  # seconds the notifier and remote owner watchers wait before their first pass

    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _sleep_until_tick_or_sync_wake(self, interval: float) -> None:
        """``_sleep_between_ticks``, cut short (within ~1s) when an applied remote
        sync change made work claimable (``kanban_db_sync.wake_dispatcher``)."""
        from hermes_cli.kanban_db_sync import consume_dispatcher_wake
        sleeper = asyncio.ensure_future(self._sleep_between_ticks(interval))
        woke = False
        try:
            while not sleeper.done():
                if consume_dispatcher_wake():
                    woke = True
                    break
                await asyncio.wait({sleeper}, timeout=1.0)
        finally:
            if not sleeper.done():
                sleeper.cancel()
        if not woke:
            await sleeper  # its result, or what it raised (stop/cancel semantics unchanged)

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        Per subscription, claims ``task_events`` newer than the stored cursor
        (kinds in TERMINAL_KINDS), sends one message per event, then advances
        the cursor. The subscription is removed only when the task is
        ``archived``: ``done`` is reversible, so the cursor — not unsubscribing
        — is the dedup mechanism (unsub-on-terminal dropped users when the
        dispatcher respawned a crashed task). All SQLite work runs in a thread;
        one tick's failure never stops the next.
        """
        try:
            from hermes_cli.config import load_config as _load_config

            cfg = _load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); continuing enabled", exc)
            kanban_cfg = {}
        if not kanban_cfg.get("notify_in_gateway", True):
            logger.info("kanban notifier: disabled via config kanban.notify_in_gateway=false")
            return

        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        sub_fail_counts: dict[tuple, int] = getattr(self, "_kanban_sub_fail_counts", {})
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name()
        self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(self._KANBAN_WATCHER_START_DELAY)

        # Stale done-sub GC: subs survive ``done``, so boards that never
        # archive would accumulate rows scanned every tick. One DELETE per
        # board, at startup (0 → first tick) and at most hourly.
        _gc_next_at = 0.0

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _retention = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    _retention = _gc_retention_days()

                deliveries = await asyncio.to_thread(
                    _notifier_collect, self, _kb,
                    notifier_profile=notifier_profile, gc_due=_gc_due, gc_retention_days=_retention,
                )
                for d in deliveries:
                    await _KanbanNotification(
                        self, d, platform_cls=_Platform, sub_fail_counts=sub_fail_counts,
                    ).deliver()
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            await self._sleep_between_ticks(interval)

    async def _kanban_remote_owner_watcher(self, interval: Optional[float] = None) -> None:
        """Admit remote cards' notices for busy chats this gateway owns (gateway/kanban_remote_owner.py). [daveey]

        Its own cadence, not the notifier loop's: inline after the local deliveries, one slow pass (or a slow
        delivery) delayed both, and the MacBook admitted busy-chat remote notices 13-88 s after it first saw
        them (t_01a1d4de28a8510d). The only caller of ``kanban_remote_owner.tick`` (it is not re-entrant).
        Stops between passes via ``_running``; a pass is never cancelled midway (a cancel during its
        ``to_thread`` collect strands claims, and inside ``admit_route_wake`` it can commit with no ``_track``).
        """
        from gateway import kanban_remote_owner
        try:
            from hermes_cli.config import load_config as _load_config

            cfg = await asyncio.to_thread(_load_config)
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception:  # health: allow BLE001 -- config is advisory here: a bad file keeps the default (enabled), logged
            logger.warning("kanban remote owner: cannot load config; continuing enabled", exc_info=True)
            kanban_cfg = {}
        if not kanban_cfg.get("notify_in_gateway", True):
            logger.info("kanban remote owner: disabled via config kanban.notify_in_gateway=false")
            return
        # The notifier's profile context (``kanban_remote_owner._profile`` falls back to it).
        if not getattr(self, "_kanban_notifier_profile", None):
            self._kanban_notifier_profile = self._active_profile_name()
        interval = kanban_remote_owner.INTERVAL_S if interval is None else interval
        await asyncio.sleep(self._KANBAN_WATCHER_START_DELAY)  # as the notifier: let adapters wire up first
        while self._running:
            await kanban_remote_owner.tick(self)  # never raises
            await self._sleep_between_ticks(interval)

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> Any:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        # Cursor writes are machine flow: the sub's board slug must resolve
        # through the env pin on a dispatcher-pinned box (same as the notifier
        # tick), or advance/rewind land on a per-slug DB the notifier never
        # reads and cursors silently reset.
        with _kb.pin_first_board_resolution():
            conn = _kbc.connect(board=board)
            try:
                return getattr(_kbn, op)(
                    conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "", **extra,
                )
            finally:
                conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)
        # Claim was durable: the send finished, so clear the in-flight holder. A crash before this
        # rewinds the claim (recover_orphaned_notify_claims) and the next tick sends again. [daveey]
        with contextlib.suppress(Exception):
            self._kanban_sub_op(board, "ack_notify_claim", sub, upto=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_drop_failed(self, sub: dict, old_cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub, preserve_withdrawal_after=old_cursor)

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task, board=None) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        """
        raw_paths: list[str] = []
        prose_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                prose_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            prose_paths += adapter.extract_local_files(str(task.result))[0]
        # A staged copy and the scratch original it was copied from are the
        # same deliverable; on a review handoff the original still exists, so
        # prose mentions of it must not upload the file a second time.
        staged_names = {os.path.basename(p) for p in raw_paths}
        raw_paths += [p for p in prose_paths if os.path.basename(p) not in staged_names]
        if isinstance(event_payload, dict) and "artifact_receipt" in event_payload:
            # New completions have explicit custody. Never fall back to prose or
            # writer-local paths, even when blob synchronization is still pending.
            from hermes_cli.kanban_artifact_delivery import delivery_paths

            records = event_payload["artifact_receipt"]
            # Digesting writer-local files reads them in full: keep it off the gateway loop.
            raw_paths = (
                await asyncio.to_thread(delivery_paths, task.id, records, board)
                if task is not None and isinstance(records, list) else []
            )
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    def _kanban_dispatcher_boot(self) -> Optional[tuple]:
        """Resolve config, kanban_db and the singleton lock; None when the dispatcher must not run.

        Config is read once at boot (restart to apply), except the auto-decompose
        toggle which is re-read every tick. The env var is an escape hatch to
        disable without editing YAML.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return None
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return None
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return None
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info("kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false")
            return None
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return None

        # The single-dispatcher lock (machine-global kanban root) is taken by the watcher through a
        # retrying, root-preferring SingletonLease (gateway/kanban_singleton.py), not here: a gateway
        # that lost the boot race used to stay non-dispatching until it restarted. [daveey]
        return _load_config, _kb, kanban_cfg

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` (default True); when false the
        loop exits and an external `hermes kanban daemon` is expected. Each
        tick runs :func:`kanban_db_dispatch.dispatch_once` in a thread; one tick's
        failure never stops the next. Shutdown: ``self._running`` is checked
        between ticks and the in-flight ``to_thread`` returns on its own.
        """
        boot = self._kanban_dispatcher_boot()
        if boot is None:
            return
        _load_config, _kb, kanban_cfg = boot
        settings = _resolve_dispatcher_settings(kanban_cfg, _kb)
        lease = self._kanban_singleton_lease(_kb.kanban_home() / "kanban" / ".dispatcher.lock", "dispatcher")
        self._kanban_dispatcher_lease = lease
        # A gateway that finds the lock held keeps retrying; a profile gateway hands it back when the
        # root gateway asks (lease.should_yield), then waits to take it again. [daveey]
        while self._running:
            if not await self._acquire_kanban_lease(lease):
                return
            self._kanban_dispatcher_lock_handle = lease.handle
            await self._kanban_dispatch_while_held(settings, _load_config, _kb, lease)

    async def _kanban_dispatch_while_held(self, settings, _load_config, _kb, lease) -> None:
        """The dispatch loop for one tenure of the singleton lock; releases it on exit."""
        interval = settings.interval
        from gateway.kanban_dispatcher_health import DispatcherHealth
        health = DispatcherHealth(getattr(self, "_kanban_dispatcher_lock_handle", None), interval=interval)
        self._kanban_dispatcher_health = health
        from hermes_cli.kanban_fairness_activation import boot_owned as boot
        held: list = []
        try:
            await _await_process_service(boot, into=held)
            # Initial delay so adapters are wired before workers spawn (matches the notifier).
            await asyncio.sleep(5)

            # Health telemetry (mirrors `_cmd_daemon`): warn when the ready queue
            # is non-empty but spawns are 0 for N consecutive ticks — usually a
            # broken PATH, missing venv, or credential loss.
            bad_ticks = 0
            last_warn_at = 0
            from hermes_cli.kanban_dispatch_expected_holds import ExpectedHoldMonitor
            hold_monitor = ExpectedHoldMonitor()
            external_hold: Optional[tuple[str, str]] = None  # (principal, since) while an external drain holds
            results: Optional[list] = None
            dispatcher = _KanbanDispatcher(_kb, settings)

            logger.info("kanban dispatcher: embedded in gateway (interval=%.1fs)", interval)
            while self._running and not lease.should_yield():
                health.invalidate()
                try:
                    # Reap zombies before per-board work so a board DB failure
                    # cannot block cleanup of unrelated workers.
                    from hermes_cli import kanban_db_dispatch as _kbd
                    pids = await _to_thread_process_service(_kbd.reap_worker_zombies)
                    if pids:
                        logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
                except Exception:  # health: allow BLE001 -- moved unchanged: zombie reaper failure is logged with traceback
                    logger.exception("kanban dispatcher: zombie reaper failed")

                try:
                    # Emergency stop (`hermes pause`): no auto-decompose or
                    # dispatch while paused; running workers finish naturally.
                    # Core-update drain hold: spawns only are suppressed; wake probes,
                    # promotion and reclaim keep running. [daveey]
                    gate = _kanban_dispatch_gate()
                    if gate is None and getattr(self, "_restart_requested", False):
                        # A deferred restart (``request_restart``'s after-turn wait) claims nothing new
                        # either: the draining owner refuses a new worker anyway, and with no hermes-sync
                        # claim hold (core declares GATEWAY_RESTART_SAFE) a claim made now only adds a
                        # run the restart must carry. A cancelled restart clears the flag. [daveey]
                        gate = "drain"
                    # An external drain (``.drain_request.json``, gateway/drain_control.py) refuses new
                    # turns the same way, so it holds claims the same way; without this the MacBook's
                    # stale-plugin restarts claimed into the drain for 48 ticks and the stuck warning
                    # blamed profile health. [daveey]
                    external = bool(getattr(self, "_external_drain_active", False))
                    if gate is None and external:
                        gate = "drain"
                    if external and external_hold is None:
                        principal, since = external_hold = await asyncio.to_thread(_external_drain_hold, time.time())
                        logger.info("kanban dispatch held: external drain (%s, since %s); "
                                    "running workers continue", principal, since)
                    elif not external and external_hold is not None:
                        principal, since = external_hold
                        logger.info("kanban dispatch released: external drain (%s, since %s) ended; "
                                    "claims resume", principal, since)
                        external_hold = None
                    if gate == "estop":
                        bad_ticks = 0
                    elif gate == "drain":
                        results = await _to_thread_process_service(
                            functools.partial(dispatcher.tick_once, suppress_spawn=True))
                        _log_spawn_results(results)
                        bad_ticks = 0  # a held ready queue is expected, not "stuck"
                    else:
                        # Re-read the auto-decompose toggle live so disabling it
                        # takes effect on the next tick, not on restart.
                        _ad_enabled, _ad_per_tick = _resolve_auto_decompose_settings(_load_config)
                        # See #49638.
                        if _ad_enabled:
                            await _to_thread_process_service(dispatcher.auto_decompose_tick, _ad_per_tick)
                        results = await _to_thread_process_service(dispatcher.tick_once)
                        if self._running and _kanban_dispatch_allowed():
                            health.publish(dispatcher.successful_scopes)
                        any_spawned = _log_spawn_results(results)
                        ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)
                        capped = _kbd.all_at_capacity(res for _slug, res in (results or []))
                        # Every ready card waiting on a known clock (quota reset, cooldown) is waiting, not stuck. [daveey]
                        clocked = _kbd.all_clock_held(res for _slug, res in (results or []))
                        # Intentional claim-quiet defer of every local candidate: explains zero spawns, only
                        # while nothing else (guards, ownership, caps, ineligibility) holds work back.
                        clocked = clocked or _kbd.all_quiet_deferred(res for _slug, res in (results or []))
                        expected = hold_monitor.observe(results or [], time.time(), logger.log)
                        bad_ticks = (bad_ticks + 1 if ready_pending and not any_spawned and
                                     not capped and not clocked and not expected else 0)
                    from hermes_cli import kanban_hub_cpu_alert  # one card per stale-advisory incident [daveey]
                    await _to_thread_process_service(kanban_hub_cpu_alert.check)
                    from hermes_cli import kanban_sync_outbox_age  # one card per stalled-outbox incident [daveey]
                    await _to_thread_process_service(kanban_sync_outbox_age.check)
                    now = int(time.time())
                    # Only an ungated tick can be "stuck": an estop or drain hold spawns 0 on purpose.
                    if gate is None and bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:
                        # Names deferred Board claims / unleasable cards when those explain the zero.
                        logger.warning("%s", _kbd.stuck_warning(bad_ticks, (res for _slug, res in (results or []))))
                        last_warn_at = now
                except asyncio.CancelledError:
                    logger.debug("kanban dispatcher: cancelled")
                    raise
                except Exception:  # health: allow BLE001 -- moved unchanged: one tick's failure never stops the next (logged with traceback)
                    health.invalidate()
                    logger.exception("kanban dispatcher: unexpected watcher error")

                await self._sleep_until_tick_or_sync_wake(interval)

        finally:
            capture = held[-1] if held else None
            try:
                if capture is not None:
                    await _await_process_service(capture.close)
            finally:
                self._release_kanban_dispatcher_lock()

    async def _kanban_sync_watcher(self) -> None:
        """Central Kanban sync (``kanban.sync.enabled``): pull the Board service's feed and push the outbox every
        ``kanban.sync.interval_seconds`` in a worker thread. One gateway per host (advisory lock), like the
        dispatcher. [daveey]"""
        from hermes_cli import kanban_db_sync as _sync
        if not _sync.sync_enabled():
            return
        from hermes_cli.kanban_sync_claims import loop_abandoned, loop_expected
        loop_expected()  # this gateway's dispatcher claims nothing until its sync loop's first round reaches the head
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_sync_client as _ksc
        # Same retrying, root-preferring lease as the dispatcher: a gateway that loses the boot race
        # keeps retrying, and a profile gateway hands the lock back when the root gateway asks. [daveey]
        lease = self._kanban_singleton_lease(_kb.kanban_home() / "kanban" / ".sync.lock", "sync")
        self._kanban_sync_lease = lease
        while self._running:
            if not await self._acquire_kanban_lease(lease):
                loop_abandoned()  # this watcher is not coming back; do not freeze claims
                return
            # Each tenure gets its own latching stop flag: after release the lease is idle again and
            # should_yield() reads False, so a bare lambda would revive this tenure's daemon FeedWatcher
            # threads beside the next tenure's set. [daveey]
            stopped = threading.Event()

            def keep_running(stopped=stopped):
                if stopped.is_set():
                    return False
                if not self._running or lease.should_yield():
                    stopped.set()
                    return False
                return True
            try:
                await asyncio.to_thread(_ksc.run_forever, keep_running)
                yielded = self._running and lease.should_yield()
            finally:
                stopped.set()
                lease.release()
            if not yielded:
                loop_abandoned()  # the loop ended and will not be respawned
                return  # the loop ended on its own (stop, or sync unavailable): don't spin on it


def sync_watcher_gave_up(_name: str = "") -> None:
    """The supervised sync watcher will not be restarted. Clear the claim hold."""
    from hermes_cli.kanban_sync_claims import loop_abandoned
    loop_abandoned()
