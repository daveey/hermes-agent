"""Root-preferring, retrying lease on the host-wide Kanban singleton locks. [daveey]

``.dispatcher.lock`` and ``.sync.lock`` live at the machine-global kanban root, so one gateway per host
dispatches and syncs every board. Before this module a gateway tried each lock once at boot: the
loser never retried, and whichever gateway reached the lock first kept it for its lifetime. On
hermes-hub a pinned profile gateway (askeve-reviewer, old core, half the root's ``max_in_progress``)
won every joint restart by ~20 s and held both locks for hours while 64 cards sat ready.

Rules:

* **Retry.** A gateway that finds the lock contended tries again every ``retry_seconds``.
* **Root preference.** The *root* gateway is the one whose HERMES_HOME is the kanban root. A profile
  gateway waits ``grace_seconds`` before its first attempt, never acquires while a live root gateway
  has posted a claim (``<lock>.root-wanted``), and gives the lock up (``should_yield``) when one
  appears. A profile gateway still dispatches/syncs on a host whose root gateway is down.
* **Visibility.** The holder writes ``<lock>.holder.json`` (pid, HERMES_HOME). A contended gateway
  logs a WARNING naming the holder once the contention passes ``warn_after_seconds``, and
  :func:`holder_info` lets ``hermes kanban stats`` and the dispatcher-presence probe name it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("gateway.run")

DEFAULT_RETRY_SECONDS = 30.0
DEFAULT_GRACE_SECONDS = 60.0
DEFAULT_WARN_AFTER_SECONDS = 300.0
_YIELD_CACHE_SECONDS = 5.0


def _resolve(path: Path) -> str:
    with contextlib.suppress(OSError):
        return str(Path(path).resolve())
    return str(path)


def is_root_gateway() -> bool:
    """True when this process's HERMES_HOME is the kanban root (not a ``profiles/<name>`` home)."""
    try:
        from hermes_cli.kanban_db import kanban_home
        from hermes_constants import get_hermes_home
        return _resolve(get_hermes_home()) == _resolve(kanban_home())
    except Exception:
        return True  # can't tell -> behave like the pre-lease code (no deference)


def _process_start(pid: int) -> Optional[int]:
    with contextlib.suppress(Exception):
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    return None


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists
        return bool(_pid_exists(pid))
    except Exception:
        return False


def _read_json(path: Path) -> Optional[dict]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    return None


def _write_json(path: Path, record: dict) -> None:
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        logger.debug("kanban singleton: cannot write %s", path, exc_info=True)


def _sidecar(lock_path: Path, suffix: str) -> Path:
    return Path(f"{lock_path}.{suffix}")


def _lock_holder_pid(lock_path: Path) -> Optional[int]:
    """Pid holding the flock on *lock_path* per ``/proc/locks`` (Linux); None elsewhere or when free."""
    try:
        st = Path(lock_path).stat()
        ident = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
        for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            # "<n>: FLOCK  ADVISORY  WRITE <pid> <maj:min:ino> 0 EOF" (blocked waiters carry "->")
            if len(fields) >= 6 and fields[1] == "FLOCK" and fields[3] == "WRITE" and fields[5] == ident:
                with contextlib.suppress(ValueError):
                    return int(fields[4])
    except OSError:
        return None
    return None


def _environ_home(pid: int) -> Optional[str]:
    with contextlib.suppress(OSError):
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if item.startswith(b"HERMES_HOME="):
                return item[len(b"HERMES_HOME="):].decode("utf-8", "replace")
    return None


def holder_info(lock_path: Path) -> Optional[dict]:
    """``{pid, hermes_home, root, since}`` for the process holding *lock_path*, or None when free/unknown.

    The pid comes from ``/proc/locks`` when readable (authoritative), else from the holder sidecar
    while that pid is alive. ``since`` is the wall time the holder took the lock (sidecar), or None.
    """
    lock_path = Path(lock_path)
    side = _read_json(_sidecar(lock_path, "holder.json")) or {}
    pid = _lock_holder_pid(lock_path)
    if pid is None:
        side_pid = side.get("pid")
        # With /proc/locks readable a free lock means no holder; elsewhere trust a live sidecar pid.
        if Path("/proc/locks").exists() or not (isinstance(side_pid, int) and _pid_alive(side_pid)):
            return None
        pid = side_pid
    matches = side.get("pid") == pid
    home = side.get("hermes_home") if matches else None
    home = home or _environ_home(pid)
    root = None
    with contextlib.suppress(Exception):
        from hermes_cli.kanban_db import kanban_home
        root = home is not None and _resolve(Path(home)) == _resolve(kanban_home())
    return {"pid": pid, "hermes_home": home, "root": root,
            "since": side.get("acquired_at") if matches else None}


def describe_holder(info: Optional[dict]) -> str:
    if not info:
        return "unknown holder"
    who = "root gateway" if info.get("root") else "profile gateway"
    return f"pid {info['pid']} ({who}, HERMES_HOME={info.get('hermes_home') or '?'})"


class SingletonLease:
    """One gateway's hold on a host-wide singleton lock. Not shared across processes; thread-safe reads.

    ``state`` is ``"held"``, ``"contended"``, ``"deferred"`` (profile gateway waiting for its grace
    period or for a live root claim), ``"unavailable"`` (no flock support: proceed on config alone),
    or ``"idle"`` (never tried / released).
    """

    def __init__(self, lock_path: Path, *, name: str, root: Optional[bool] = None,
                 retry_seconds: float = DEFAULT_RETRY_SECONDS, grace_seconds: float = DEFAULT_GRACE_SECONDS,
                 warn_after_seconds: float = DEFAULT_WARN_AFTER_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.lock_path = Path(lock_path)
        self.name = name
        self.root = is_root_gateway() if root is None else bool(root)
        self.retry_seconds = float(retry_seconds)
        self.grace_seconds = 0.0 if self.root else float(grace_seconds)
        self.warn_after_seconds = float(warn_after_seconds)
        self._clock = clock
        self._created = clock()
        self.handle: Any = None
        self.state = "idle"
        self._contended_since: Optional[float] = None
        self._last_warn: Optional[float] = None
        self._logged_contended = False
        self._yield_cache: tuple[float, bool] = (float("-inf"), False)
        self._lock = threading.Lock()
        self.token = uuid.uuid4().hex  # identifies this lease's claim/holder records (two leases may share a pid)

    # --- root claim ---------------------------------------------------------------------------------

    @property
    def _claim_path(self) -> Path:
        return _sidecar(self.lock_path, "root-wanted")

    def _post_root_claim(self) -> None:
        pid = os.getpid()
        _write_json(self._claim_path, {"pid": pid, "token": self.token, "started_at": _process_start(pid),
                                       "posted_at": time.time()})

    def _clear_own_root_claim(self) -> None:
        claim = _read_json(self._claim_path)
        if claim and claim.get("token") == self.token:
            with contextlib.suppress(OSError):
                self._claim_path.unlink()

    def root_claim_live(self) -> bool:
        """A live root gateway (another lease) has asked for the lock."""
        claim = _read_json(self._claim_path)
        if not claim or claim.get("token") == self.token:
            return False
        pid = claim.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            return False
        started = claim.get("started_at")
        return started is None or _process_start(pid) in (None, started)

    # --- acquire / release --------------------------------------------------------------------------

    def try_acquire(self) -> bool:
        """One attempt. True when this gateway may run the singleton loop (``held`` or ``unavailable``)."""
        if self.state in ("held", "unavailable"):
            return True
        if not self.root:
            if self._clock() - self._created < self.grace_seconds or self.root_claim_live():
                self.state = "deferred"
                return False
        from gateway.kanban_watchers_common import _acquire_singleton_lock
        handle, state = _acquire_singleton_lock(self.lock_path)
        if state == "held":
            self.handle, self.state = handle, "held"
            pid = os.getpid()
            home: Optional[str] = None
            with contextlib.suppress(Exception):
                from hermes_constants import get_hermes_home
                home = str(get_hermes_home())
            _write_json(_sidecar(self.lock_path, "holder.json"), {
                "pid": pid, "token": self.token, "started_at": _process_start(pid), "acquired_at": time.time(),
                "hermes_home": home, "root": self.root})
            if self.root:
                self._clear_own_root_claim()
            waited = (self._clock() - self._contended_since) if self._contended_since is not None else None
            self._contended_since = None
            self._logged_contended = False
            logger.info("kanban %s: holding singleton %s lock (%s)%s", self.name, self.name, self.lock_path,
                        f" after {waited:.0f}s of contention" if waited else "")
            return True
        if state == "unavailable":
            self.state = "unavailable"
            logger.warning("kanban %s: advisory lock unavailable at %s; proceeding on config control alone.",
                           self.name, self.lock_path)
            return True
        self.state = "contended"
        now = self._clock()
        if self._contended_since is None:
            self._contended_since = now
        if self.root:
            self._post_root_claim()
        if not self._logged_contended:
            self._logged_contended = True
            logger.info("kanban %s: another gateway holds %s (%s); retrying every %.0fs%s", self.name,
                        self.lock_path, describe_holder(holder_info(self.lock_path)), self.retry_seconds,
                        " and asking a profile holder to yield" if self.root else "")
        self._maybe_warn(now)
        return False

    def _maybe_warn(self, now: float) -> None:
        if self._contended_since is None or now - self._contended_since < self.warn_after_seconds:
            return
        if self._last_warn is not None and now - self._last_warn < self.warn_after_seconds:
            return
        self._last_warn = now
        info = holder_info(self.lock_path)
        logger.warning("kanban %s: %s has held %s for %.0fs while this %s gateway waits; this gateway's "
                       "kanban.* settings (max_in_progress, profiles) are not the ones in effect", self.name,
                       describe_holder(info), self.lock_path, now - self._contended_since,
                       "root" if self.root else "profile")

    def should_yield(self) -> bool:
        """Holder side: True when a profile gateway should hand the lock to a live root claimant."""
        if self.root or self.state != "held":
            return False
        with self._lock:
            at, cached = self._yield_cache
            now = self._clock()
            if now - at < _YIELD_CACHE_SECONDS:
                return cached
            result = self.root_claim_live()
            self._yield_cache = (now, result)
        if result:
            logger.info("kanban %s: root gateway asked for %s; this profile gateway yields it",
                        self.name, self.lock_path)
        return result

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            side = _sidecar(self.lock_path, "holder.json")
            rec = _read_json(side)
            if rec and rec.get("token") == self.token:
                with contextlib.suppress(OSError):
                    side.unlink()
            from gateway.kanban_watchers_common import _release_singleton_lock
            _release_singleton_lock(handle)
        if self.root:
            self._clear_own_root_claim()
        self.state = "idle"
        self._yield_cache = (float("-inf"), False)


# --- restart ordering ----------------------------------------------------------------------------
# A pinned profile gateway running an older core neither defers nor yields, so on a joint restart the
# only lever is order: start the root gateway, wait until it holds the host locks it takes, then start
# the profile gateways (``hermes_cli.update_cmd_posix_pause.resume_paused_set``). [daveey]

ROOT_LOCK_WAIT_SECONDS = 90.0


def is_root_home(home: Optional[str]) -> bool:
    if not home:
        return False
    try:
        from hermes_cli.kanban_db import kanban_home
        return _resolve(Path(home)) == _resolve(kanban_home())
    except Exception:
        return False


def root_expected_locks() -> list[Path]:
    """The host locks a root gateway takes under the kanban root's own config.yaml."""
    from hermes_cli.kanban_db import kanban_home
    root = kanban_home() / "kanban"
    locks: list[Path] = []
    try:
        from hermes_cli.config_backend import config_exists, read_config_doc_readonly
        path = kanban_home() / "config.yaml"
        kanban = ((read_config_doc_readonly(path) if config_exists(path) else None) or {}).get("kanban") or {}
    except Exception:
        kanban = {}
    if kanban.get("dispatch_in_gateway", True):
        locks.append(root / ".dispatcher.lock")
    if (kanban.get("sync") or {}).get("enabled"):
        locks.append(root / ".sync.lock")
    return locks


def wait_for_root_locks(pid: int, *, timeout: float = ROOT_LOCK_WAIT_SECONDS, locks: Optional[list] = None,
                        sleep: Optional[Callable[[float], None]] = None,
                        clock: Optional[Callable[[], float]] = None) -> list[str]:
    """Block until *pid* holds every expected host lock, or *timeout*; returns the lock names it lacks.

    Never raises: a lock held by another live process is reported (that holder will not yield if it runs
    an older core) and the caller carries on.
    """
    try:
        paths = root_expected_locks() if locks is None else [Path(p) for p in locks]
    except Exception:
        return []
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout
    while True:
        missing = [p for p in paths if (holder_info(p) or {}).get("pid") != pid]
        if not missing or clock() >= deadline or not _pid_alive(pid):
            return [p.name for p in missing]
        sleep(1.0)
