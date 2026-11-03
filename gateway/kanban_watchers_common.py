"""Plumbing shared by the kanban notifier and dispatcher loops.

Thread offload, board enumeration, live-config coercers and the singleton
dispatcher lock live here so the notifier, dispatcher and mixin modules read
them from one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import Context
from pathlib import Path
from typing import Any, Callable, Optional

# Keep the logger name run.py used so extracted log records are unchanged.
logger = logging.getLogger("gateway.run")


def _run_in_fresh_context(func: Callable[..., Any], /, *args: Any) -> Any:
    """Run *func* in an empty ``Context`` so request-local ContextVars stay behind.

    ``asyncio.to_thread`` copies the caller's context; a lingering
    ``delegate_task`` child marker would make ``write_txn`` false-trip for
    these process-owned writers. An empty Context keeps the DB guard intact
    for real children without exempting dispatcher writes.
    """
    return Context().run(func, *args)


async def _to_thread_process_service(func: Callable[..., Any], /, *args: Any) -> Any:
    """Offload blocking process-service work without inheriting request ContextVars."""
    return await asyncio.to_thread(_run_in_fresh_context, func, *args)


def _list_boards(kb: Any) -> list:
    """Enumerate live boards; fall back to the default board when listing fails."""
    try:
        return kb.list_boards(include_archived=False)
    except Exception:
        return [kb.read_board_metadata(kb.DEFAULT_BOARD)]


def _board_slugs(kb: Any) -> list:
    return [b.get("slug") or kb.DEFAULT_BOARD for b in _list_boards(kb)]


def _positive_int_setting(kanban_cfg: dict, key: str) -> Optional[int]:
    """Parse an optional ``kanban.<key>`` int cap; None when unset or invalid (< 1 is invalid)."""
    raw = kanban_cfg.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.%s=%r; ignoring", key, raw)
        return None
    if value < 1:
        logger.warning("kanban dispatcher: kanban.%s=%r is below 1; ignoring", key, raw)
        return None
    logger.info("kanban dispatcher: %s=%d", key, value)
    return value


def _resolve_auto_decompose_settings(load_config: Callable[[], Any]) -> tuple[bool, int]:
    """Live (enabled, per_tick) auto-decompose settings, re-read every dispatcher tick.

    Fails safe: a config read error returns ``(False, 3)`` rather than
    re-enabling a feature the user turned off. ``per_tick`` is clamped to ``>= 1``.

    Read fresh from config on every dispatcher tick (#49638) so that flipping ``kanban.auto_decompose:
    false`` to STOP runaway fan-out takes effect on the next tick instead of requiring a gateway restart.
    Auto-decompose is a safety toggle — a user who sees it create and launch tasks they didn't intend
    reaches for this flag to halt it, and a stale boot-captured value silently ignoring that change is the
    bug reported in #49638.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    return bool(kcfg.get("auto_decompose", True)), max(per_tick, 1)


AUTO_DECOMPOSE_MIN_AGE_DEFAULT = 120


def _auto_decompose_min_age_seconds() -> int:
    """``kanban.auto_decompose_min_age_seconds`` (default 120; 0 = sweep immediately), re-read per tick.

    The auto-decompose sweep skips triage cards younger than this so a creator can park a triage
    hold with ``kanban_schedule`` before it is specified and dispatched (fixture cards were specified
    6-16 s after creation and ran, defect kanban-76c77018a6). Manual decompose/specify ignore it.
    A config read error keeps the default window. [daveey]
    """
    try:
        from hermes_cli.config import load_config

        raw = ((load_config() or {}).get("kanban") or {}).get(
            "auto_decompose_min_age_seconds", AUTO_DECOMPOSE_MIN_AGE_DEFAULT)
        return max(int(AUTO_DECOMPOSE_MIN_AGE_DEFAULT if raw is None else raw), 0)
    except Exception:
        logger.warning("kanban.auto_decompose_min_age_seconds unreadable; using %ss",
                       AUTO_DECOMPOSE_MIN_AGE_DEFAULT, exc_info=True)
        return AUTO_DECOMPOSE_MIN_AGE_DEFAULT


def _gc_retention_days() -> int:
    """``kanban.done_sub_retention_days`` (default 30; 0 disables), re-read per sweep; fails safe to 30."""
    try:
        from hermes_cli.config import load_config

        return int(((load_config() or {}).get("kanban") or {}).get("done_sub_retention_days", 30))
    except Exception:
        return 30


def _kanban_dispatch_gate() -> Optional[str]:
    """Why this tick may not spawn: ``"estop"``, ``"drain"`` or ``None`` (free to dispatch).

    ``"estop"`` (`hermes pause`) skips the whole tick. ``"drain"`` (hermes-sync core-update drain
    hold) only suppresses NEW claims: wake probes, promotion, stale-claim reclaim and health keep
    running, so parked chains don't stall for the length of the hold. [daveey]
    Fails open when either module is unimportable.
    """
    try:
        from agent.estop import check_paused
    except ImportError:
        return None
    if check_paused("kanban", logger):
        return "estop"
    try:
        from agent.core_update_drain import check_drain_hold
    except ImportError:
        return None
    return "drain" if check_drain_hold("kanban", logger) else None


def _kanban_dispatch_allowed() -> bool:
    """False while the emergency stop or a core-update drain hold forbids NEW claims.

    Checked every tick before spawning, so a pause applies on the next tick;
    in-flight workers are never touched.
    """
    return _kanban_dispatch_gate() is None


def _acquire_singleton_lock(lock_path) -> tuple[Optional[object], str]:
    """Take the exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway machine-wide may run the embedded dispatcher: concurrent
    dispatchers double reclaim frequency and claim events, and with
    ``wal_autocheckpoint=0`` concurrent manual checkpoints can corrupt index
    pages. ``dispatch_in_gateway`` is the primary control; this is the backstop.

    Returns ``(handle, "held")`` (release via :func:`_release_singleton_lock`),
    ``(None, "contended")`` when another process holds it (caller must NOT
    dispatch), or ``(None, "unavailable")`` when locking cannot be performed
    (caller falls back to config control).
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")  # windows-footgun: ok (append-mode lock handle, write not read)
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _is_kanban_root_profile() -> bool:
    """True when this process's HERMES_HOME is the kanban root (the default profile), not a named profile whose
    home only borrows the root's boards. [daveey]"""
    try:
        from hermes_cli import kanban_db
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve() == Path(kanban_db.kanban_home()).resolve()
    except Exception:  # health: allow BLE001 -- an unresolvable home keeps today's behaviour (may sync)
        return True


def _root_gateway_alive() -> bool:
    """True when the kanban root profile's gateway runs on this host (its lock held, its PID live).

    Read-only probe of the root's identity files (``cleanup_stale=False``): a named-profile process never unlinks
    the root's records. [daveey]"""
    try:
        from gateway.status import get_running_pid
        from hermes_cli import kanban_db
        return get_running_pid(Path(kanban_db.kanban_home()) / "gateway.pid", cleanup_stale=False) is not None
    except Exception:  # health: allow BLE001 -- unknown reads as absent: the profile gateway may keep syncing
        return False


def _release_singleton_lock(handle) -> None:
    """Release a lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    with contextlib.suppress(Exception):
        from gateway.status import _release_file_lock

        _release_file_lock(handle)
    with contextlib.suppress(Exception):
        handle.close()
