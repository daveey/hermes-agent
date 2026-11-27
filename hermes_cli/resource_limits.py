"""Best-effort process resource-limit adjustments for long-running services."""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import stat
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping
from typing import Any

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.launch_malloc import server_argv as server_process_argv

try:  # ``resource`` is POSIX-only (and unavailable on Windows).
    import resource as _resource
except (ImportError, ModuleNotFoundError):  # pragma: no cover - Windows only
    _resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_NOFILE_SOFT_LIMIT = int(DEFAULT_CONFIG["runtime"]["nofile_soft_limit"])
DEFAULT_MALLOC_ARENA_MAX = int(DEFAULT_CONFIG["runtime"]["malloc_arena_max"])
_M_ARENA_MAX = -8  # glibc <malloc.h> mallopt parameter
_MISSING = object()


def _runtime_int(config: Mapping[str, Any] | None, key: str, default: int) -> int | None:
    """Positive int ``runtime.<key>`` from a loaded config, or ``None`` when disabled/unresolvable.

    Missing key → *default*. Explicit ``0``/``false``/``null`` disable; other non-int or negative
    values are ignored (caller fails open).
    """
    if config is None:
        try:
            # Profile-aware loader (applies managed-scope overlays and defaults).
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not load config for runtime.%s", key, exc_info=True)
            return None
    if not isinstance(config, Mapping):
        return None
    runtime = config.get("runtime", _MISSING)
    if runtime is _MISSING:
        return default
    if not isinstance(runtime, Mapping):
        return None
    raw_value = runtime.get(key, _MISSING)
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value <= 0:
        return None
    return raw_value


def configured_nofile_soft_limit(config: Mapping[str, Any] | None = None) -> int | None:
    """``runtime.nofile_soft_limit`` (see :func:`_runtime_int`). Shared by the in-process floor and
    service-definition generators (launchd plist) so both use one knob."""
    return _runtime_int(config, "nofile_soft_limit", DEFAULT_NOFILE_SOFT_LIMIT)


def apply_nofile_soft_limit(config: Mapping[str, Any] | None = None) -> bool:
    """Best-effort raise of this process's ``RLIMIT_NOFILE`` soft limit; ``True`` iff changed.

    Target = ``runtime.nofile_soft_limit`` (default :data:`DEFAULT_NOFILE_SOFT_LIMIT`), clamped
    to a finite hard limit; never lowers a higher soft limit. Unsupported platforms, malformed
    settings, and denied ``setrlimit`` must never prevent a server from starting.
    """
    if _resource is None:
        return False
    target = configured_nofile_soft_limit(config)
    if target is None:
        return False
    try:
        nofile = _resource.RLIMIT_NOFILE
        current_soft, current_hard = _resource.getrlimit(nofile)
        # RLIM_INFINITY may be -1, which ordinary ordering would treat as "lower than any
        # target"; never replace infinity with a finite limit.
        infinity = getattr(_resource, "RLIM_INFINITY", object())
        if current_soft == infinity or current_soft >= target:
            return False
        new_soft = target if current_hard == infinity else min(target, current_hard)
        if new_soft <= current_soft:
            return False
        _resource.setrlimit(nofile, (new_soft, current_hard))
        return True
    except Exception:
        logger.debug("Could not raise RLIMIT_NOFILE soft limit", exc_info=True)
        return False


_fd_samples: deque[tuple[float, int]] = deque(maxlen=1801)
_fd_last_check: float | None = None
_fd_pid = os.getpid()
_fd_lock = threading.Lock()


def _fd_kind(name, directory, platform):
    if platform == 'darwin':
        import fcntl
        fd = int(name)
        mode = os.fstat(fd).st_mode
        if stat.S_ISSOCK(mode):
            return 'socket'
        if stat.S_ISFIFO(mode):
            return 'pipe'
        target = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024)).split('\0', 1)[0]
    else:
        target = os.readlink(f'{directory}/{name}')
    target = target.removesuffix(' (deleted)')
    if '/.resource-leases/' in target:
        return 'resource-leases'
    if target.endswith(('.db', '-wal', '-shm')):
        return 'sqlite'
    if target.startswith(('socket:', 'socket[')):
        return 'socket'
    if target.startswith(('pipe:', 'pipe[')):
        return 'pipe'
    return 'other'


def _fd_snapshot(soft: int, *, clock=None, platform=None) -> tuple[int, dict]:
    """Bound classification by both elapsed work and descriptors; tolerate close races."""
    clock = clock or time.monotonic
    platform = platform or sys.platform
    directory = '/dev/fd' if platform == 'darwin' else '/proc/self/fd'
    names = os.listdir(directory)
    count = len(names)
    if count <= soft * .25:
        return count, {}
    census: Counter[str] = Counter()
    started, examined = clock(), 0
    for name in names:
        if examined >= 2000 or clock() - started >= .5:
            break
        examined += 1
        try:
            kind = _fd_kind(name, directory, platform)
        except (OSError, ValueError):
            kind = 'other'  # Closed/racing descriptors are still part of sampled count.
        census[kind] += 1
    return count, dict(classes=dict(census), partial=examined < count,
                       examined=examined, coverage=examined / count if count else 1.0)


def _fd_core_head() -> str:
    # This is the loaded revision, not the possibly updated checkout on disk.
    from gateway.code_skew import _boot_fingerprint, _process_boot_fingerprint
    fingerprint = _boot_fingerprint or _process_boot_fingerprint()
    return fingerprint.rsplit(':', 1)[-1] if fingerprint else 'unknown'


def _notify_fd_incident(sample: dict, *, probe: bool = False, probe_board=None) -> dict:
    from hermes_cli.fd_headroom_incident import report
    return report(sample, probe=probe, probe_board=probe_board)


_fd_status: dict = {}
_fd_incident: dict = {}
_fd_config_warning: float | None = None
_FD_PUBLIC_FIELDS = frozenset(('state', 'sampled_at', 'count', 'soft', 'hard', 'ratio',
                              'last_card_id', 'create_state', 'failing_since', 'error_class', 'skipped'))


def fd_headroom_status() -> dict:
    """Public health boundary: never export census paths, delivery payloads or error text."""
    return {key: value for key, value in _fd_status.items() if key in _FD_PUBLIC_FIELDS}


_fd_thread: threading.Thread | None = None
_fd_request_lock = threading.Lock()


def request_fd_headroom(*, role: str) -> None:
    """Nonblocking service tick: retain one worker, count overlap instead of queueing work."""
    global _fd_thread
    try:
        with _fd_request_lock:
            if _fd_thread is not None and _fd_thread.is_alive():
                _fd_status['skipped'] = _fd_status.get('skipped', 0) + 1
                return
            import contextvars
            scope = contextvars.copy_context()
            def run():
                try:
                    if role == 'gateway':
                        gateway_fd_headroom_tick()
                    else:
                        check_fd_headroom(role=role)
                except Exception as exc:  # health: allow BLE001 -- status publication cannot kill the worker
                    logger.warning('fd-headroom: tick failed: %s', exc)
                    _fd_status.update(error_class=type(exc).__name__)
            _fd_thread = threading.Thread(target=scope.run, args=(run,), name='fd-headroom', daemon=True)
            _fd_thread.start()
    except Exception as exc:  # health: allow BLE001 -- thread exhaustion must not kill a server tick
        _fd_status.update(error_class=type(exc).__name__)
        logger.warning('fd-headroom: could not schedule check: %s', exc)


def gateway_fd_headroom_tick() -> None:
    """The gateway's existing housekeeping thread owns this process-wide tick."""
    sample = check_fd_headroom(role='gateway')
    if sample is not None:
        from gateway.status import publish_runtime_status
        publish_runtime_status(fd_headroom=fd_headroom_status())


def check_fd_headroom(config: Mapping[str, Any] | None = None, *, role: str = 'backend',
                      now: float | None = None, wall_now: float | None = None,
                      probe: bool = False, probe_board: str | None = None) -> dict | None:
    """Due-gated own-process check. Service callers schedule it off-loop, outside board locks.

    One in flight per process; an overlapping tick is counted, never queued. The reporter
    additionally serializes the host/profile/role identity across processes.
    """
    if not _fd_lock.acquire(blocking=False):
        _fd_status['skipped'] = _fd_status.get('skipped', 0) + 1
        return None
    try:
        return _check_fd_headroom(config, role, time.monotonic() if now is None else now,
                                  time.time() if wall_now is None else wall_now, probe, probe_board)
    except Exception as exc:  # health: allow BLE001 -- diagnostics must never escape a service tick
        _fd_status.update(error_class=type(exc).__name__)
        logger.warning('fd-headroom: check failed: %s', exc)
        return None
    finally:
        _fd_lock.release()


def _raise_fd_soft(sample, maximum):
    soft, hard = sample['soft'], sample['hard']
    target = min(maximum, soft * 4)
    if hard != _resource.RLIM_INFINITY:
        target = min(hard, target)
    sample['state'] = 'capped'
    if target <= soft:
        sample.update(error_class='LimitCap', raise_error='hard or max_soft cap')
        return
    try:
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (target, hard))
        observed, observed_hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        if observed != target or observed_hard != hard:
            raise OSError('soft limit readback did not confirm target')
        sample.update(state='raised', raised=f'fd-headroom: raised soft {soft}→{observed}')
        logger.warning('%s', sample['raised'])
    except (OSError, ValueError, AttributeError) as exc:
        sample.update(error_class=type(exc).__name__, raise_error=str(exc))


def _check_fd_headroom(config, role, now, wall, probe, probe_board):
    global _fd_last_check, _fd_pid, _fd_config_warning, _fd_incident
    from hermes_cli.fd_headroom_config import validated
    if _resource is None:
        return None
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    settings, errors = validated(config)
    if not settings['enabled']:
        return None
    if _fd_pid != os.getpid():
        _fd_pid, _fd_last_check = os.getpid(), None
        _fd_samples.clear()
        _fd_incident = {}
    if _fd_last_check is not None and now - _fd_last_check < settings['interval_seconds']:
        return None
    _fd_last_check = now
    if errors and (_fd_config_warning is None or now - _fd_config_warning >= 3600):
        logger.warning('fd-headroom: invalid config keys %s; using defaults', errors)
        _fd_config_warning = now
    soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    if soft <= 0 or soft == _resource.RLIM_INFINITY:
        return None
    count, census = _fd_snapshot(soft)
    while _fd_samples and _fd_samples[0][0] < now - 1800:
        _fd_samples.popleft()
    _fd_samples.append((now, count))
    elapsed = now - _fd_samples[0][0]
    slope = ((count - _fd_samples[0][1]) * 60 / elapsed
             if len(_fd_samples) >= 4 and elapsed >= 1200 else 0.0)
    predicted = slope > 0 and (soft - count) / slope <= settings['horizon_hours'] * 60
    warn = count >= soft * settings['warn_ratio'] or predicted
    sample = dict(host=socket.gethostname(), pid=os.getpid(), role=role, count=count,
                  soft=soft, hard=hard, census=census, slope=slope, core=_fd_core_head(), raised='',
                  state='warn' if warn else 'ok', triggered=bool(warn), error_class=None)
    if count >= soft * settings['raise_ratio']:
        _raise_fd_soft(sample, settings['max_soft'])  # Before any card I/O; warn uses original sample.
    if (warn or _fd_incident.get('envelope') or _fd_status.get('last_card_id')
            or _fd_status.get('create_state') == 'failing'):
        try:
            kwargs = {'probe': probe}
            if probe_board is not None:
                kwargs['probe_board'] = probe_board
            _fd_incident = _notify_fd_incident(sample, **kwargs) or {}
        except Exception as exc:  # health: allow BLE001 -- even unwritable reporter state cannot hide census
            _fd_incident.setdefault('failing_since', wall)
            _fd_incident.update(error=str(exc), error_class=type(exc).__name__)
    from hermes_cli.fd_headroom_incident import failure_note
    note = failure_note(_fd_incident, now=wall)
    _fd_status.update(state='config_error' if errors else sample['state'], sampled_at=wall,
                      count=count, soft=soft, hard=hard, ratio=count / soft,
                      last_card_id=_fd_incident.get('task_id'),
                      create_state='failing' if _fd_incident.get('error') else 'ok',
                      failing_since=_fd_incident.get('failing_since'),
                      error_class='ConfigError' if errors else (
                          sample['error_class'] or _fd_incident.get('error_class')))
    if warn or note:
        logger.warning('fd-headroom: host=%s pid=%s role=%s count=%s soft=%s hard=%s '
                       'census=%s slope=%.6f fd/min core=%s %s %s', sample['host'], sample['pid'],
                       role, count, soft, hard, census, slope, sample['core'],
                       sample.get('raise_error', ''), note)
    return sample


def glibc_malloc() -> Any | None:
    """This process's libc handle when it is glibc with ``mallopt`` (else ``None``: musl, macOS, Windows)."""
    try:
        libc = ctypes.CDLL(None)
    except (OSError, TypeError):  # Windows: no process-global symbol namespace
        return None
    return libc if hasattr(libc, "gnu_get_libc_version") and hasattr(libc, "mallopt") else None


def _arena_max_setting(env: Mapping[str, str]) -> str | None:
    """The arena cap an environment asks glibc for (exact ``glibc.malloc.arena_max`` tunable, else
    ``MALLOC_ARENA_MAX``), or ``None``."""
    for item in env.get("GLIBC_TUNABLES", "").split(":"):
        name, sep, value = item.partition("=")
        if sep and name == "glibc.malloc.arena_max":
            return value
    return env.get("MALLOC_ARENA_MAX")


def _launch_environ() -> dict[str, str] | None:
    """The environment this process was exec'd with (what glibc read at startup), or ``None``.

    ``/proc/self/environ`` keeps the exec-time block; ``os.environ`` also holds values Hermes loaded
    later from the profile ``.env``, which glibc never saw.
    """
    try:
        raw = open("/proc/self/environ", "rb").read()
    except OSError:
        return None
    entries = (os.fsdecode(entry).partition("=") for entry in raw.split(b"\0") if entry)
    return {name: value for name, sep, value in entries if sep}


def _positive_c_int(value: str) -> int | None:
    try:
        number = int(value.strip())
    except ValueError:
        return None
    return number if 0 < number <= 2**31 - 1 else None


def apply_malloc_arena_max(config: Mapping[str, Any] | None = None) -> bool:
    """Cap glibc's malloc arena count for this process (``mallopt(M_ARENA_MAX)``); ``True`` iff set.

    glibc gives every thread that contends on an arena lock a new arena, up to 8 x cores, and never
    unmaps them. A long-lived server with hundreds of short-lived Python threads plus native pools
    (tokio, rayon, sqlite) fills every slot (157 of 160 on a 20-core host), and each arena keeps its
    own free pages. The GIL already serialises Python's allocations, so a small cap costs no
    measurable throughput. Applied in-process rather than via an exported ``MALLOC_ARENA_MAX`` so
    exec'd children (terminal commands, MCP servers, builds) keep glibc's default.

    Call before the process starts any thread: arenas that exist never go away, and glibc latches its
    limit once a thread asks for an arena past ``glibc.malloc.arena_test``. (The gateway's startup
    watchdog thread arms before config loads, so a gateway may hold one arena beyond a cap of 1.)

    An arena setting in the exec-time environment was already applied by glibc and wins. One that
    is only in ``os.environ`` because Hermes loaded it from the profile ``.env`` is applied here with
    the operator's number (glibc never read it). glibc only; never raises.
    """
    libc = glibc_malloc()
    if libc is None:
        return False
    current = _arena_max_setting(os.environ)
    launch_env = _launch_environ()
    launched = current if launch_env is None else _arena_max_setting(launch_env)
    if launched is not None and current in (None, launched):
        return False
    if current is not None:
        target = _positive_c_int(current)
    else:
        target = _runtime_int(config, "malloc_arena_max", DEFAULT_MALLOC_ARENA_MAX)
        target = target if target is not None and target <= 2**31 - 1 else None
    if target is None:
        return False
    try:
        return bool(libc.mallopt(_M_ARENA_MAX, target))
    except Exception:
        logger.debug("Could not cap malloc arenas", exc_info=True)
        return False


__all__ = [
    "DEFAULT_MALLOC_ARENA_MAX", "DEFAULT_NOFILE_SOFT_LIMIT", "apply_malloc_arena_max",
    "apply_nofile_soft_limit", "check_fd_headroom", "configured_nofile_soft_limit", "glibc_malloc", "server_process_argv"]
