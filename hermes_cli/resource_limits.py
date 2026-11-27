"""Best-effort process resource-limit adjustments for long-running services."""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import subprocess
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


def _fd_snapshot(soft: int) -> tuple[int, dict[str, int]]:
    """Count actual entries, then classify only above 25%; closed/racing entries are harmless."""
    for directory in ('/proc/self/fd', '/dev/fd'):
        try:
            names = os.listdir(directory)
            break
        except FileNotFoundError:
            continue
    else:
        raise OSError('no process fd directory')
    census: Counter[str] = Counter()
    if len(names) > soft * 0.25:
        for name in names:
            try:
                target = os.readlink(f'{directory}/{name}').removesuffix(' (deleted)')
            except OSError:
                continue
            if '/.resource-leases/' in target:
                kind = 'resource-leases'
            elif target.endswith(('.db', '-wal', '-shm')):
                kind = 'sqlite'
            elif target.startswith(('socket:', 'socket[')):
                kind = 'socket'
            elif target.startswith(('pipe:', 'pipe[')):
                kind = 'pipe'
            else:
                kind = 'other'
            census[kind] += 1
    return len(names), dict(census)


def _fd_core_head() -> str:
    from hermes_cli._startup_fast import project_root_str
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=project_root_str(),
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def _notify_fd_incident(sample: dict, *, probe: bool = False) -> dict:
    from hermes_cli.fd_headroom_incident import report
    return report(sample, probe=probe)


_fd_status: dict = {}


def fd_headroom_status() -> dict:
    """Latest census and incident-delivery state for existing runtime status readers."""
    return dict(_fd_status)


def gateway_fd_headroom_tick() -> None:
    """The gateway's existing housekeeping thread owns this process-wide tick."""
    sample = check_fd_headroom(role='gateway')
    if sample is not None:
        from gateway.status import publish_runtime_status
        publish_runtime_status(fd_headroom=fd_headroom_status())


def check_fd_headroom(config: Mapping[str, Any] | None = None, *, role: str = 'backend',
                      now: float | None = None, probe: bool = False) -> dict | None:
    """Due-gated, never-raising own-process census/rescue. Call from existing service ticks.

    Samples cover 30 minutes; the endpoint slope projects time to the current soft limit.
    A host-shared reporter serializes incident cards across profiles and processes. ``probe``
    uses a separate incident namespace and [probe] title, never a production incident.
    """
    try:
        with _fd_lock:
            return _check_fd_headroom(config, role, time.monotonic() if now is None else now, probe)
    except Exception:  # health: allow BLE001 -- diagnostics must never escape a service tick
        logger.debug('fd-headroom: check failed', exc_info=True)
        return None


def _check_fd_headroom(config, role, now, probe):
    global _fd_last_check, _fd_pid
    if _resource is None:
        return None
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    settings = {**DEFAULT_CONFIG['runtime']['fd_headroom'], **config.get('runtime', {}).get('fd_headroom', {})}
    if settings['enabled'] is not True:
        return None
    if _fd_pid != os.getpid():
        _fd_pid, _fd_last_check = os.getpid(), None
        _fd_samples.clear()
    interval = max(1.0, float(settings['interval_seconds']))
    if _fd_last_check is not None and now - _fd_last_check < interval:
        return None
    _fd_last_check = now
    soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    if soft <= 0 or soft == _resource.RLIM_INFINITY:
        return None
    count, census = _fd_snapshot(soft)
    while _fd_samples and _fd_samples[0][0] < now - 1800:
        _fd_samples.popleft()
    _fd_samples.append((now, count))
    elapsed = now - _fd_samples[0][0]
    slope = (count - _fd_samples[0][1]) / elapsed if elapsed > 0 else 0.0
    predicted = slope > 0 and (soft - count) / slope <= float(settings['horizon_hours']) * 3600
    warn = count >= soft * float(settings['warn_ratio']) or predicted
    sample = dict(host=socket.gethostname(), pid=os.getpid(), role=role, count=count,
                  soft=soft, hard=hard, census=census, slope=slope, core=_fd_core_head(), raised='')
    if count >= soft * float(settings['raise_ratio']):
        target = soft * 4 if hard == _resource.RLIM_INFINITY else min(hard, soft * 4)
        if target > soft:
            try:
                _resource.setrlimit(_resource.RLIMIT_NOFILE, (target, hard))
                sample['raised'] = f'fd-headroom: raised soft {soft}→{target}'
            except (OSError, ValueError) as exc:
                sample['raised'] = f'fd-headroom: raise failed: {exc}'
            logger.warning('%s', sample['raised'])
    sample['triggered'] = bool(warn or sample['raised'])
    incident = _fd_status.get('incident', {})
    if sample['triggered'] or incident:
        try:
            incident = _notify_fd_incident(sample, probe=probe) or {}
        except Exception as exc:  # health: allow BLE001 -- even unwritable reporter state must not hide the census
            incident = {**incident, 'error': str(exc)[:500],
                        'failure_since': incident.get('failure_since', time.time())}
    from hermes_cli.fd_headroom_incident import failure_note
    note = failure_note(incident)
    _fd_status.update(sample, incident=incident, card_error=note)
    log = logger.warning if warn or note else logger.info
    log('fd-headroom: host=%s pid=%s role=%s count=%s soft=%s hard=%s census=%s slope=%.6f fd/s core=%s %s',
        sample['host'], sample['pid'], role, count, soft, hard, census, slope, sample['core'], note)
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
