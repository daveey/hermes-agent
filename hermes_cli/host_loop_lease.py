"""One owner per Hermes home for host-wide background loops (design t_01a1d45d75851804 §2a). [daveey]

The gateway, every Desktop ``serve`` backend and the dashboard each start the same background loops
(unloaded sweeps, producer admission, chat-closeout, annotation refresh). Their passes are host-wide
work, so N processes do it N times. A loop that adopts this lease runs its host pass only while
``lease.held()``::

    lease = HostLoopLease("kanban-unloaded", "serve", interval=30)
    while not stopped.wait(30):
        if lease.held():
            run_host_pass()
            lease.beat()

The lock is ``flock(LOCK_EX | LOCK_NB)`` on ``<realpath(root home)>/run/host-loops/<name>.lock``. The
key is the profile-tree ROOT home (the passes iterate the root and ``profiles/*``), never a profile
home, so profile processes of one install share one owner.

* **Death.** The kernel drops the lock when the holder's last descriptor closes, so a killed holder
  frees it at once and a standby takes it on its next ``held()`` (one interval). The fd is opened
  ``O_CLOEXEC``: an exec'd child never inherits it. A ``fork()`` WITHOUT exec shares the open file
  description, and a flock belongs to that description, so the child would keep the lock alive after
  the parent dies. :func:`_after_fork_child` therefore closes the child's copy and resets its state
  (never ``LOCK_UN`` there: that would release the parent's lock too).
* **Metadata.** The holder writes pid, process start time, role, core sha and ``heartbeat_at`` into the
  lock file; :meth:`HostLoopLease.beat` refreshes the heartbeat once per pass.
* **Watchdog.** A pass that runs longer than ``3 × interval`` without a ``beat()`` makes the holder's
  watchdog thread release the lock, so one wedged pass cannot stop the host loop. The wedged pass may
  still finish concurrently with the new holder's pass; that is safe because callers claim their work
  by cursor CAS / ledger rows. A standby that reads a stale heartbeat logs one WARNING per holder.
* **Priority** gateway > Desktop serve > dashboard. A standby of higher priority than the holder writes
  ``<name>.want`` (pid + start time, verified alive) and blocks on ``LOCK_EX`` in a daemon thread, so it
  gets the lock the moment it is released (no polling gap). The holder reads ``.want`` in ``beat()`` at
  the end of each pass and releases. Any other standby that wins the lock in that release window drops
  it again while a live ``.want`` of priority >= its own exists.
* **Kill switch** ``HERMES_HOST_LOOP_LEASE=0``: ``held()`` is always True (every process runs its own
  pass, the behaviour before this lease).
* **Unavailable lock** (no ``fcntl``, a directory nobody can create the file in, a filesystem without
  flock): the lease fails OPEN, ``held()`` True, logged once. Duplicated passes are today's safe
  behaviour; failing closed would stop the host loops on every process of that home. An existing lock
  file this process may not write is opened read-only (flock works on it, as in ``update_lock``); such a
  holder holds the lock but cannot record metadata.

``hermes doctor`` lists the holders (:func:`holders`).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
import weakref
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no flock; the lease fails open (every process runs its passes).
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

KILL_SWITCH_ENV = "HERMES_HOST_LOOP_LEASE"  # health: allow HX002 -- the card's required kill switch
WATCHDOG_FACTOR = 3
ROLE_PRIORITY = {"gateway": 3, "serve": 2, "dashboard": 1}
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_LEASES: weakref.WeakSet = weakref.WeakSet()


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip() != "0"


def lease_dir(home: str | Path | None = None) -> Path:
    """``<realpath(root home)>/run/host-loops``: one directory per install, shared by its profiles."""
    from hermes_constants import get_default_hermes_root
    return Path(os.path.realpath(get_default_hermes_root(home=home))) / "run" / "host-loops"


def _start_of(pid: int):
    try:
        from gateway.status import _get_process_start_time
        return _get_process_start_time(int(pid))
    except (ImportError, OSError, ValueError, TypeError):
        return None


def _alive(pid, start) -> bool:
    """True unless ``pid`` is provably gone (dead, or recycled with another start time)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except PermissionError:
        pass  # exists, owned by someone else
    except (OSError, ValueError, OverflowError):
        return False
    if start is None:
        return True
    current = _start_of(pid)
    if current is None:
        return True
    try:
        from gateway.status import start_time_fingerprints_match
        return start_time_fingerprints_match(start, current)
    except (TypeError, ValueError):
        return False


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_bytes() or b"null")
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


_ME: tuple | None = None
_SHA: list = []


def _me() -> tuple:
    """``(pid, start-time fingerprint)`` of this process, cached per pid (a fork re-reads it)."""
    global _ME
    if _ME is None or _ME[0] != os.getpid():
        _ME = (os.getpid(), _start_of(os.getpid()))
    return _ME


def _core_sha() -> str | None:
    """The code this process booted from (read once)."""
    if not _SHA:
        try:
            from hermes_cli.version_info import get_code_identity
            sha = get_code_identity().get("sha")
        except Exception:  # health: allow BLE001 -- metadata only: a broken identity probe must never stop a host loop
            logger.debug("host loop lease: code identity unavailable", exc_info=True)
            sha = None
        _SHA.append(sha if isinstance(sha, str) and sha else None)
    return _SHA[0]


def _spawn(target, name: str) -> None:
    thread = threading.Thread(target=contextvars.copy_context().run, args=(target,), name=name, daemon=True)
    thread.start()


class HostLoopLease:
    """A per-home flock lease for one host loop; see the module docstring."""

    def __init__(self, name: str, role: str, *, interval: float, home: str | Path | None = None) -> None:
        if not _NAME_RE.fullmatch(name or ""):
            raise ValueError(f"host loop lease name must match {_NAME_RE.pattern}: {name!r}")
        if not interval or interval <= 0:
            raise ValueError("interval must be > 0 seconds")
        self.name, self.role, self.interval = name, role, float(interval)
        self.priority = ROLE_PRIORITY.get(role, 0)
        self.directory = lease_dir(home)
        self.path = self.directory / f"{name}.lock"
        self.want_path = self.directory / f"{name}.want"
        self._mu = threading.RLock()
        self._fd: int | None = None
        self._writable = False
        self._held = False
        self._waiting = False        # a daemon thread blocks on LOCK_EX for a .want hand-off
        self._cancel_wait = False    # release() while waiting: drop the lock when the thread gets it
        self._stop: threading.Event | None = None  # the current holding's watchdog stop flag
        self._last_beat = 0.0
        self._acquired_at = 0.0
        self._next_probe = 0.0
        self._next_open = 0.0
        self._unavailable_logged = False
        self._stale_warned: tuple | None = None
        _LEASES.add(self)

    # -- public API ----------------------------------------------------------------------------

    def held(self) -> bool:
        """True when this process should run the host pass now. One ``flock`` syscall when not holder."""
        if not enabled() or self._held:
            return True
        if self._waiting:
            return False
        with self._mu:
            if self._held:
                return True
            if self._waiting:
                return False
            fd = self._fd if self._fd is not None else self._open()
            if fd is None:
                return True  # fail open (logged once)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._standby_probe()
                return False
            except OSError as exc:  # ENOLCK and friends: no usable lock on this filesystem
                self._unavailable(f"flock failed: {exc}")
                return True
            want = self._live_want()
            if want is not None and want.get("pid") != os.getpid() and want.get("priority", 0) >= self.priority:
                fcntl.flock(fd, fcntl.LOCK_UN)  # a higher-priority waiter is due this lock
                return False
            self._become_holder()
            return True

    def beat(self) -> None:
        """End of a pass: refresh the heartbeat, then hand off to a live higher-priority ``.want``."""
        if not enabled() or not self._held:
            return
        with self._mu:
            if not self._held:
                return
            self._last_beat = time.monotonic()
            self._write_meta(held=True)
            want = self._live_want()
            if want is not None and want.get("pid") != os.getpid() and want.get("priority", 0) > self.priority:
                logger.info("host loop %s: handing the lease to %s pid %s", self.name, want.get("role"),
                            want.get("pid"))
                self._drop()

    def release(self) -> None:
        """Give the lease up (shutdown). A later ``held()`` competes for it again."""
        with self._mu:
            if self._held:
                self._drop()
            self._clear_own_want()
            if self._waiting:
                self._cancel_wait = True  # the blocked thread drops the lock and closes the fd
                return
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    # -- internals -----------------------------------------------------------------------------

    def _unavailable(self, why: str) -> None:
        self._next_open = time.monotonic() + self.interval
        if not self._unavailable_logged:
            self._unavailable_logged = True
            logger.warning("host loop %s: lease %s unavailable (%s); running this process's own pass",
                           self.name, self.path, why)

    def _open(self) -> int | None:
        if fcntl is None:
            self._unavailable("no fcntl on this platform")
            return None
        if time.monotonic() < self._next_open:
            return None
        flags = os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # the file may still exist and open
        try:
            self._fd, self._writable = os.open(self.path, os.O_RDWR | os.O_CREAT | flags, 0o644), True
        except OSError as exc:
            try:  # flock works on a read-only fd (a lock file another user left behind)
                self._fd, self._writable = os.open(self.path, os.O_RDONLY | flags), False
            except OSError:
                self._unavailable(exc.strerror or str(exc))
                return None
        return self._fd

    def _become_holder(self) -> None:
        self._held = True
        self._acquired_at = time.time()
        self._last_beat = time.monotonic()
        self._write_meta(held=True)
        stop = self._stop = threading.Event()
        _spawn(lambda: self._watch(stop), f"host-loop-watchdog-{self.name}")
        logger.info("host loop %s: this %s process holds the lease", self.name, self.role)

    def _drop(self) -> None:
        """Release a held lock (caller holds ``_mu``). Metadata first: after LOCK_UN it is the next holder's."""
        self._write_meta(held=False)
        self._held = False
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _watch(self, stop: threading.Event) -> None:
        limit = WATCHDOG_FACTOR * self.interval
        while not stop.wait(min(self.interval, limit) / 2):
            with self._mu:
                if stop.is_set() or not self._held:
                    return
                silent = time.monotonic() - self._last_beat
                if silent > limit:
                    logger.warning("host loop %s: no beat() for %.1fs (> %d x %.1fs interval); releasing the "
                                   "lease so a standby runs the next pass", self.name, silent, WATCHDOG_FACTOR,
                                   self.interval)
                    self._drop()
                    return

    def _write_meta(self, *, held: bool) -> None:
        if not self._writable or self._fd is None:
            return
        now = time.time()
        meta = {"name": self.name, "held": held, "pid": _me()[0], "start": _me()[1],
                "role": self.role, "priority": self.priority, "sha": _core_sha(), "interval": self.interval,
                "acquired_at": self._acquired_at, "heartbeat_at": now}
        if not held:
            meta["released_at"] = now
        data = json.dumps(meta).encode()
        try:
            os.pwrite(self._fd, data, 0)
            os.ftruncate(self._fd, len(data))
        except OSError as exc:
            logger.debug("host loop %s: metadata write failed: %s", self.name, exc)

    def _live_want(self) -> dict | None:
        want = _read_json(self.want_path)
        if want is None or not _alive(want.get("pid"), want.get("start")):
            return None
        return want

    def _clear_own_want(self) -> None:
        want = _read_json(self.want_path)
        if want is not None and want.get("pid") == os.getpid():
            try:
                self.want_path.unlink()
            except OSError:
                pass

    def _standby_probe(self) -> None:
        """At most once per interval: warn on a stale holder, and claim a lower-priority holder's lease."""
        now = time.monotonic()
        if now < self._next_probe:
            return
        self._next_probe = now + self.interval
        meta = _read_json(self.path)
        if not meta or not meta.get("held") or not _alive(meta.get("pid"), meta.get("start")):
            return
        holder = (meta.get("pid"), meta.get("start"))
        interval = float(meta.get("interval") or self.interval)
        age = time.time() - float(meta.get("heartbeat_at") or 0)
        if age > WATCHDOG_FACTOR * interval and self._stale_warned != holder:
            self._stale_warned = holder
            logger.warning("host loop %s: holder %s pid %s last beat %.0fs ago (> %d x %.0fs interval)",
                           self.name, meta.get("role"), holder[0], age, WATCHDOG_FACTOR, interval)
        if int(meta.get("priority") or 0) >= self.priority:
            return
        want = self._live_want()
        if want is not None and want.get("pid") != os.getpid() and want.get("priority", 0) >= self.priority:
            return  # another process of at least our priority is already next
        self._want()

    def _want(self) -> None:
        """Publish ``.want`` and block on LOCK_EX in a daemon thread (caller holds ``_mu``)."""
        if not self._writable or self._fd is None:
            return
        record = {"pid": _me()[0], "start": _me()[1], "role": self.role,
                  "priority": self.priority, "at": time.time()}
        tmp = self.want_path.with_name(f".{self.want_path.name}.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(record), encoding="utf-8")
            os.replace(tmp, self.want_path)
        except OSError as exc:
            logger.debug("host loop %s: cannot write %s: %s", self.name, self.want_path, exc)
            return
        self._waiting, self._cancel_wait = True, False
        fd = self._fd
        logger.info("host loop %s: %s outranks the holder; waiting for its hand-off", self.name, self.role)
        _spawn(lambda: self._wait_for_lock(fd), f"host-loop-want-{self.name}")

    def _wait_for_lock(self, fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            logger.debug("host loop %s: blocking acquire failed: %s", self.name, exc)
            with self._mu:
                self._waiting = False
                self._clear_own_want()  # a live .want nobody waits on would hold every standby off
            return
        with self._mu:
            self._waiting = False
            if self._fd != fd:  # forked child state reset or fd replaced: not ours any more
                return
            if self._cancel_wait:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                self._fd = None
                return
            self._become_holder()
            self._clear_own_want()


def holders(home: str | Path | None = None) -> list[dict]:
    """Every lease of this home: its last metadata plus ``live`` (a live process says it holds it)."""
    directory = lease_dir(home)
    try:
        paths = sorted(directory.glob("*.lock"))
    except OSError:
        return []
    out = []
    for path in paths:
        meta = _read_json(path) or {}
        meta.setdefault("name", path.stem)
        meta["live"] = bool(meta.get("held")) and _alive(meta.get("pid"), meta.get("start"))
        out.append(meta)
    return out


def _after_fork_child() -> None:
    """A forked child shares the parent's lock (one open file description): close its copy, never LOCK_UN."""
    for lease in list(_LEASES):
        if lease._fd is not None:
            try:
                os.close(lease._fd)
            except OSError:
                pass
        lease._fd, lease._held, lease._waiting, lease._stop = None, False, False, None
        lease._mu = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
