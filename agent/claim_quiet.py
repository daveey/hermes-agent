"""Root-scoped claim-quiet lease: a bounded, non-renewable, lifetime-locked request that local
automatic claim admission pause while an observer measures the gateway.

Layout under ``<root>/kanban/claim-quiet/`` (``root`` is ``agent.file_safety._hermes_root_path()``
called at use time, never a profile home): permanent ``producer.lock`` (its inode is never replaced
or unlinked), atomic ``last-started.json`` (2h start cooldown) and atomic ``lease.json``.

Two deliberately opposite defaults:

* **Admission is fail-open.** :func:`read_active` returns a lease only when everything verifies; a
  missing, malformed, expired, unlocked, dead-producer, wrong-boot or ambiguous lease returns ``None``.
* **Observation is fail-closed.** :func:`inspect` keeps *why* it is not active; only
  ``state == "active"`` is a valid observation, so ``"inactive"`` and ``"uncertain"`` both refuse it.

``flock`` cannot prove which PID owns a lock. The lease therefore also demands a live producer PID
whose start identity matches, the same lock inode, the same boot and an unreached wall AND
suspend-inclusive deadline. A faulty descendant that keeps the lock descriptor after the producer
dies cannot extend suppression (the PID check fails); it can only keep a successor producer out until
it exits.

Fork custody: every lease descriptor a producer creates is registered in ``_CUSTODY`` under a lock
that the ``os.register_at_fork`` hooks also take, so another thread's fork cannot interleave with
open/registration or deregistration/close. The child closes every registered descriptor (never
``LOCK_UN``, which would release the parent's open file description) and every owner becomes inert.
A fork by the SAME thread inside such a window (a signal handler) cannot be serialized: both copies
then refuse the acquisition (``QuietUnavailableError``) and the child never acts as the producer.

Trusted-ancestry limit: the lease directory is re-bound to its pathname at specific points (open,
pre-publication, post-publication, reader verification), which detects a rename or replacement
between those checks. A same-user mutator that swaps the path right after the last check cannot be
fenced (there is no atomic global namespace guarantee), and the ancestors of the lease directory are
trusted not to be replaced.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # unsupported platform: acquire() refuses, readers report uncertain
    fcntl = None  # type: ignore[assignment]

from agent import claim_quiet_records as records
from agent.claim_quiet_records import (
    COOLDOWN_S, LAST_STARTED_NAME, LEASE_NAME, LOCK_NAME, MAX_LEASE_S, MAX_REASON_CHARS,
    PRODUCER_RETRY_S, LastStarted, QuietClock, QuietLease, RecordError, SystemClock)
from hermes_platform import quiet_clock
from hermes_platform.quiet_clock import ClockSampleError

logger = logging.getLogger(__name__)

_NS = 1_000_000_000
_FUTURE_SKEW_S = 1.0

__all__ = [
    "QuietBusyError", "QuietCooldownError", "QuietLease", "QuietLeaseError", "QuietOwner",
    "QuietPaths", "QuietReading", "QuietUnavailableError", "acquire", "inspect", "quiet_paths",
    "read_active",
]


class QuietLeaseError(RuntimeError):
    """Base of every refusal to start a lease."""


class QuietUnavailableError(QuietLeaseError):
    """Platform, filesystem, clock or record state cannot support a trustworthy lease."""


class QuietBusyError(QuietLeaseError):
    """Another producer holds the lock (or contention outlived the bounded retry)."""


class QuietCooldownError(QuietLeaseError):
    def __init__(self, remaining_s: float):
        super().__init__(f"claim-quiet cooldown: {remaining_s:.0f}s remaining")
        self.remaining_s = remaining_s


@dataclass(frozen=True)
class QuietPaths:
    root: Path
    directory: Path
    lock: Path
    last_started: Path
    lease: Path


@dataclass(frozen=True)
class QuietReading:
    """``active`` (lease verified), ``inactive`` (certainly no suppression) or ``uncertain``."""

    state: str
    lease: QuietLease | None
    reason: str


class _Stop(Exception):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state, self.reason = state, reason


def quiet_paths() -> QuietPaths:
    from agent import file_safety

    root = Path(file_safety._hermes_root_path())
    directory = root / "kanban" / "claim-quiet"
    return QuietPaths(root, directory, directory / LOCK_NAME, directory / LAST_STARTED_NAME,
                      directory / LEASE_NAME)


# ---- identity helpers -------------------------------------------------------------------------

_incarnation_cache: tuple[int, str] | None = None


def _incarnation() -> str:
    """Random per-process-incarnation id; a forked child gets its own."""
    global _incarnation_cache
    pid = os.getpid()
    if _incarnation_cache is None or _incarnation_cache[0] != pid:
        _incarnation_cache = (pid, secrets.token_hex(16))
    return _incarnation_cache[1]


def _process_start(pid: int) -> float:
    """Canonical start identity of ``pid`` (``hermes_cli.process_identity.worker_birth``).

    Raises ``psutil.NoSuchProcess`` for a missing or zombie process, other ``psutil.Error`` when
    the process cannot be read.
    """
    import psutil

    from hermes_cli.process_identity import worker_birth

    proc = psutil.Process(pid)
    if proc.status() == psutil.STATUS_ZOMBIE:
        raise psutil.NoSuchProcess(pid)
    return worker_birth(proc)


# ---- directory / lock file --------------------------------------------------------------------


def _close_quiet(fd: int, original: BaseException | None = None) -> None:
    """Close a descriptor on a failure path without letting a close error replace the failure."""
    try:
        os.close(fd)
    except OSError as exc:
        logger.debug("claim-quiet descriptor close failed", exc_info=True)
        if original is not None:
            original.add_note(f"descriptor close also failed: {exc!r}")


def _open_dir(paths: QuietPaths, *, create: bool, track: bool = False) -> int:
    if create:
        paths.directory.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):
            os.mkdir(paths.directory, 0o700)
    fd = os.open(paths.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:  # windows-footgun: ok -- POSIX-only; unsupported hosts refuse before this
            raise PermissionError(errno.EACCES, "claim-quiet directory is not private to this user")
    except BaseException as exc:
        _close_quiet(fd, exc)
        raise
    if track:
        _CUSTODY.add(fd)
    return fd


def _open_lock(dir_fd: int, *, create: bool, track: bool = False) -> int:
    flags = os.O_CLOEXEC | os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
    fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=dir_fd)
    try:
        _require_stable_lock(dir_fd, fd)
    except BaseException as exc:
        _close_quiet(fd, exc)
        raise
    if track:
        _CUSTODY.add(fd)
    return fd


def _require_stable_lock(dir_fd: int, fd: int) -> os.stat_result:
    """The descriptor is the regular, owned file currently at ``producer.lock`` (not a replacement)."""
    held = os.fstat(fd)
    at_path = os.stat(LOCK_NAME, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid():  # windows-footgun: ok -- POSIX-only; unsupported hosts refuse before this
        raise PermissionError(errno.EACCES, "producer.lock is not an owned regular file")
    if (held.st_dev, held.st_ino) != (at_path.st_dev, at_path.st_ino):
        raise OSError(errno.ESTALE, "producer.lock inode was replaced")
    return held


def _require_bound(paths: QuietPaths, dir_fd: int, root_real: str) -> None:
    """The opened directory is still what ``<root>/kanban/claim-quiet`` names, and the root still
    resolves where it did. Detects a rename/replacement before this check, nothing after it."""
    if os.path.realpath(paths.root) != root_real:
        raise OSError(errno.ESTALE, "claim-quiet root no longer resolves to the same path")
    held = os.fstat(dir_fd)
    at_path = os.stat(paths.directory, follow_symlinks=False)
    if not stat.S_ISDIR(at_path.st_mode) or (held.st_dev, held.st_ino) != (at_path.st_dev, at_path.st_ino):
        raise OSError(errno.ESTALE, "claim-quiet directory was detached from its path")


# ---- reader -----------------------------------------------------------------------------------


def _probe_held(fd: int) -> bool:
    """Shared nonblocking probe: True when someone holds the exclusive lock. Never waits, and a
    successful probe is released at once so it can only delay a producer for an instant."""
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        raise _Stop("uncertain", f"lock probe failed: {exc.strerror}") from exc
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def _load_lease(dir_fd: int) -> QuietLease | None:
    try:
        raw = records.read_record(dir_fd, LEASE_NAME)
        return None if raw is None else records.parse_lease(raw)
    except RecordError as exc:
        raise _Stop("uncertain", f"lease record: {exc}") from exc


def _check_producer(lease: QuietLease) -> None:
    import psutil

    try:
        started = _process_start(lease.producer_pid)
    except psutil.NoSuchProcess:
        raise _Stop("inactive", "producer process is gone") from None
    except (psutil.Error, OSError) as exc:
        raise _Stop("uncertain", f"producer identity unreadable: {type(exc).__name__}") from exc
    if started != lease.producer_started_at:
        raise _Stop("inactive", "producer PID now belongs to another process")


def _check_time(lease: QuietLease, clock: QuietClock, now: float | None) -> None:
    try:  # every sampled value is validated before any ordering comparison can be fooled by it
        wall, elapsed, boot = records.sample(clock, now)
    except ClockSampleError as exc:
        raise _Stop("uncertain", f"clock sample unusable: {exc}") from exc
    if boot != lease.boot_id:
        raise _Stop("uncertain", "lease belongs to another boot")
    if elapsed < lease.started_ns:
        raise _Stop("uncertain", "elapsed clock is earlier than the lease start")
    if lease.started_at > wall + _FUTURE_SKEW_S:
        raise _Stop("uncertain", "lease start is in the future")
    if elapsed >= lease.deadline_ns or wall >= lease.expires_at:
        raise _Stop("inactive", "lease expired")


def _inspect_locked(paths: QuietPaths, dir_fd: int, fd: int, clock: QuietClock,
                    now: float | None, root_real: str | None = None) -> QuietLease:
    root_real = root_real or os.path.realpath(paths.root)
    if not _probe_held(fd):
        raise _Stop("inactive", "producer lock is not held")
    lease = _load_lease(dir_fd)
    if lease is None:
        raise _Stop("inactive", "lock held but no lease published")
    held = os.fstat(fd)
    if (lease.lock_dev, lease.lock_ino) != (held.st_dev, held.st_ino):
        raise _Stop("uncertain", "lease names a different lock inode")
    if lease.root != root_real:
        raise _Stop("uncertain", "lease belongs to another root")
    _check_time(lease, clock, now)
    _check_producer(lease)
    # Busy is necessary, not sufficient: the same episode must still own the same inode and lock.
    if _load_lease(dir_fd) != lease:
        raise _Stop("uncertain", "lease changed while it was read")
    try:
        _require_stable_lock(dir_fd, fd)
        _require_bound(paths, dir_fd, root_real)
    except OSError as exc:
        raise _Stop("uncertain", f"lock or directory identity changed: {exc.strerror}") from exc
    if not _probe_held(fd):
        raise _Stop("inactive", "producer lock was released while it was read")
    return lease


def _inspect(clock: QuietClock, now: float | None) -> QuietReading:
    if fcntl is None or not quiet_clock.lease_supported():
        raise _Stop("uncertain", "claim-quiet lease unsupported on this host")
    paths = quiet_paths()
    root_real = os.path.realpath(paths.root)
    try:
        dir_fd = _open_dir(paths, create=False)
    except FileNotFoundError:
        raise _Stop("inactive", "no claim-quiet directory") from None
    except OSError as exc:
        raise _Stop("uncertain", f"directory unusable: {exc.strerror}") from exc
    try:
        try:
            _require_bound(paths, dir_fd, root_real)
            fd = _open_lock(dir_fd, create=False)
        except FileNotFoundError:
            raise _Stop("inactive", "no lock file") from None
        except OSError as exc:
            raise _Stop("uncertain", f"lock file unusable: {exc.strerror}") from exc
        try:
            lease = _inspect_locked(paths, dir_fd, fd, clock, now, root_real)
        finally:
            _close_quiet(fd)
    finally:
        _close_quiet(dir_fd)
    return QuietReading("active", lease, "verified")


def inspect(*, now: float | None = None, clock: QuietClock | None = None) -> QuietReading:
    """Observer-grade read: the verdict plus why. Never writes, repairs or deletes anything."""
    try:
        return _inspect(clock or SystemClock(), now)
    except _Stop as stop:
        return QuietReading(stop.state, None, stop.reason)
    except Exception as exc:  # health: allow BLE001 -- admission must fail open on any reader fault
        logger.debug("claim-quiet inspect failed", exc_info=True)
        return QuietReading("uncertain", None, f"unexpected {type(exc).__name__}")


def read_active(*, now: float | None = None, clock: QuietClock | None = None) -> QuietLease | None:
    """The verified active lease, else ``None`` (admission open) for every other outcome."""
    return inspect(now=now, clock=clock).lease


# ---- producer ---------------------------------------------------------------------------------
#
# Fork custody. ``_CUSTODY`` holds every descriptor this process's producers own. ``_FORK_LOCK`` is
# taken by the ``register_at_fork`` hooks and by every "custody section" (open+register, and
# deregister+close), so another thread's fork lands strictly before or after a section. The hooks
# acquire and release it in the forking thread, so the lock stays consistent in the child.

_FORK_LOCK = threading.RLock()
_tls = threading.local()
_CUSTODY: set[int] = set()
_OWNERS: dict[int, QuietOwner] = {}
_section_forks = 0  # forks taken by a thread that was itself inside a custody section


@contextmanager
def _custody_section():
    with _FORK_LOCK:
        _tls.depth = getattr(_tls, "depth", 0) + 1
        try:
            yield
        finally:
            _tls.depth -= 1


def _note_section_fork() -> None:
    global _section_forks
    if getattr(_tls, "fork_in_section", False):
        _tls.fork_in_section = False
        _section_forks += 1


def _fork_prepare() -> None:
    _FORK_LOCK.acquire()  # re-entrant: a same-thread (signal handler) fork cannot deadlock here
    if getattr(_tls, "depth", 0) > 0:
        _tls.fork_in_section = True


def _fork_parent() -> None:
    try:
        _note_section_fork()
    finally:
        _FORK_LOCK.release()


def _fork_child() -> None:
    """Close every inherited lease descriptor WITHOUT ``LOCK_UN`` (that would release the parent's
    open file description) and make every owner inert; the child must never act as the producer."""
    try:
        _note_section_fork()
        for fd in tuple(_CUSTODY):
            with suppress(OSError):
                os.close(fd)
        _CUSTODY.clear()
        for owner in tuple(_OWNERS.values()):
            owner._abandon_in_child()
        _OWNERS.clear()
    finally:
        _FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_fork_prepare, after_in_parent=_fork_parent, after_in_child=_fork_child)


def _release_fd(fd: int) -> None:
    """Deregister and close ``fd`` as one fork-atomic step. Skipped when the fork hook already
    closed it. A failed close is never retried: its descriptor state is ambiguous (reuse hazard)."""
    with _custody_section():
        if fd not in _CUSTODY:
            return
        _CUSTODY.discard(fd)
        os.close(fd)


def _raise_first(errors: list[BaseException]) -> None:
    if errors:
        first = errors[0]
        for extra in errors[1:]:
            first.add_note(f"additional cleanup failure: {extra!r}")
        raise first


class QuietOwner:
    """The producer's hold on the lease; :meth:`close` is idempotent and unconditional."""

    def __init__(self, lock_fd: int, dir_fd: int):
        self._lock_fd, self._dir_fd = lock_fd, dir_fd
        self._pid = os.getpid()
        self._mutex = threading.Lock()
        self._closed = False
        self._abandoned = False
        self.lease: QuietLease | None = None
        with _custody_section():
            _OWNERS[id(self)] = self

    @property
    def closed(self) -> bool:
        return self._closed

    def _abandon_in_child(self) -> None:
        # The fork hook already closed the descriptors. The mutex may have been held by a thread
        # that does not exist in the child, so it is replaced, never acquired.
        self._closed = self._abandoned = True
        self._mutex = threading.Lock()

    def _live(self) -> tuple[int, int]:
        """``(lock_fd, dir_fd)`` while this process still owns them, else refuse."""
        if self._closed or self._abandoned or os.getpid() != self._pid:
            raise QuietUnavailableError("claim-quiet owner is not live in this process")
        return self._lock_fd, self._dir_fd

    def _clear_own_lease(self) -> None:
        if self.lease is None:
            return
        with suppress(OSError, RecordError):  # an unremovable record is inert once unlocked
            raw = records.read_record(self._dir_fd, LEASE_NAME)
            if raw is not None and records.parse_lease(raw).episode_id == self.lease.episode_id:
                os.unlink(LEASE_NAME, dir_fd=self._dir_fd)

    def close(self) -> None:
        if self._abandoned or os.getpid() != self._pid:
            # A fork child (or an owner the hook abandoned): the inherited mutex is never touched
            # and the shared open file description is never unlocked.
            self._closed = True
            for fd in (self._lock_fd, self._dir_fd):
                with suppress(OSError):
                    _release_fd(fd)
            return
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            errors: list[BaseException] = []
            # Each step is attempted regardless of the others; the lock is released before either
            # descriptor leaves custody, so a fork in between inherits only an unlocked description.
            for step, arg in ((self._clear_own_lease, None),
                              (lambda: fcntl.flock(self._lock_fd, fcntl.LOCK_UN), None),
                              (_release_fd, self._lock_fd), (_release_fd, self._dir_fd)):
                try:
                    step() if arg is None else step(arg)
                except BaseException as exc:  # health: allow BLE001 -- every step must be attempted
                    errors.append(exc)
            with _custody_section():
                _OWNERS.pop(id(self), None)
            _raise_first(errors)

    def __enter__(self) -> QuietOwner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _validate_request(max_s: object, reason: object) -> tuple[int, str]:
    if type(max_s) is not int or not 1 <= max_s <= MAX_LEASE_S:
        raise ValueError(f"max_s must be an integer in 1..{MAX_LEASE_S}")
    if type(reason) is not str or not (text := reason.strip()) or len(text) > MAX_REASON_CHARS \
            or not text.isprintable():
        raise ValueError(f"reason must be nonempty printable text of at most {MAX_REASON_CHARS} chars")
    return max_s, text


def _take_lock(owner: QuietOwner, clock: QuietClock) -> None:
    """Exclusive nonblocking lock with bounded retry. A competing producer holding a verified lease
    is refused at once; transient probe contention (readers' shared locks) is retried <= 1s."""
    deadline = time.monotonic() + PRODUCER_RETRY_S
    delay = 0.002
    while True:
        try:
            fcntl.flock(owner._live()[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        held = inspect(clock=clock)
        if held.state == "active":
            raise QuietBusyError(f"another claim-quiet lease is active (episode {held.lease.episode_id})")
        if time.monotonic() >= deadline:
            raise QuietBusyError("claim-quiet lock contended beyond the bounded retry")
        time.sleep(delay)  # bounded by PRODUCER_RETRY_S; probes hold the lock for microseconds
        delay = min(delay * 2, 0.05)


def _check_cooldown(dir_fd: int, wall: float, ns: int, boot: str) -> None:
    try:
        raw = records.read_record(dir_fd, LAST_STARTED_NAME)
        last = None if raw is None else records.parse_last_started(raw)
    except RecordError as exc:
        raise QuietUnavailableError(f"cooldown record unusable, refusing: {exc}") from exc
    if last is None:
        return
    # Same boot: the monotonic age alone is authoritative, so a wall clock that was ahead when the
    # record was written (and later corrected) cannot refuse every start until real time catches up.
    # Other boot: only the wall age exists.
    if last.boot_id == boot:
        age = (ns - last.started_ns) / _NS
    else:
        age = wall - last.started_at
    if age < 0:
        raise QuietUnavailableError("cooldown clocks are incoherent, refusing")
    remaining = COOLDOWN_S - age
    if remaining > 0:
        raise QuietCooldownError(remaining)


def _publish(owner: QuietOwner, paths: QuietPaths, root_real: str, max_s: int, reason: str,
             clock: QuietClock) -> None:
    """Cooldown commit FIRST, then the lease; a failure after the cooldown write still consumes it.
    Everything sampled or constructed is validated before the first write."""
    wall, ns, boot = records.sample(clock)
    lock_fd, dir_fd = owner._live()
    lock_st = _require_stable_lock(dir_fd, lock_fd)
    _require_bound(paths, dir_fd, root_real)
    with suppress(FileNotFoundError):  # we hold the lock, so any lease record is a dead one's
        os.unlink(LEASE_NAME, dir_fd=dir_fd)
    _check_cooldown(dir_fd, wall, ns, boot)
    import psutil

    try:
        started = _process_start(os.getpid())
    except (psutil.Error, OSError) as exc:
        raise QuietUnavailableError(f"cannot establish producer identity: {exc}") from exc
    episode = secrets.token_hex(16)
    lease = QuietLease(
        episode_id=episode, producer_pid=os.getpid(), producer_started_at=started,
        producer_incarnation=_incarnation(), boot_id=boot, started_at=wall,
        expires_at=wall + max_s, started_ns=ns, deadline_ns=ns + max_s * _NS, max_s=max_s,
        reason=reason, root=root_real, lock_dev=lock_st.st_dev, lock_ino=lock_st.st_ino)
    cooldown_bytes = records.dump_checked(LastStarted(episode, wall, ns, boot))
    lease_bytes = records.dump_checked(lease)
    records.write_atomic(owner._live()[1], LAST_STARTED_NAME, cooldown_bytes)
    records.write_atomic(owner._live()[1], LEASE_NAME, lease_bytes)
    owner.lease = lease  # assigned before the final checks so a failure there clears what was written
    lock_fd, dir_fd = owner._live()
    _require_stable_lock(dir_fd, lock_fd)
    _require_bound(paths, dir_fd, root_real)


def _open_owner(paths: QuietPaths) -> QuietOwner:
    """Open, register and wrap both descriptors in ONE custody section (so another thread's fork
    sees them registered), refusing when this thread itself was forked inside the section."""
    forks = _section_forks
    with _custody_section():
        dir_fd = _open_dir(paths, create=True, track=True)
        try:
            lock_fd = _open_lock(dir_fd, create=True, track=True)
        except BaseException as exc:
            try:
                _release_fd(dir_fd)
            except OSError as cleanup:
                exc.add_note(f"descriptor close also failed: {cleanup!r}")
            raise
        if forks != _section_forks or lock_fd not in _CUSTODY or dir_fd not in _CUSTODY:
            errors: list[BaseException] = []
            for fd in (lock_fd, dir_fd):
                try:
                    _release_fd(fd)
                except OSError as exc:
                    errors.append(exc)
            refusal = QuietUnavailableError("fork occurred inside claim-quiet descriptor custody; refusing")
            for exc in errors:
                refusal.add_note(f"descriptor close also failed: {exc!r}")
            raise refusal
        return QuietOwner(lock_fd, dir_fd)


def _start(owner: QuietOwner, paths: QuietPaths, root_real: str, clock: QuietClock, max_s: int,
           reason: str) -> None:
    _take_lock(owner, clock)
    _publish(owner, paths, root_real, max_s, reason, clock)


def _fail_start(owner: QuietOwner, exc: BaseException) -> None:
    """Release the owner, keeping the ORIGINAL failure primary; a cleanup failure is attached."""
    try:
        owner.close()
    except Exception as cleanup:  # health: allow BLE001 -- the original start failure must win
        logger.warning("claim-quiet cleanup failed after a failed start", exc_info=True)
        exc.add_note(f"claim-quiet cleanup also failed: {cleanup!r}")
    if isinstance(exc, (RecordError, ClockSampleError)):
        raise QuietUnavailableError(f"claim-quiet start refused: {exc}") from exc
    if isinstance(exc, OSError):
        raise QuietUnavailableError(f"claim-quiet start failed: {exc.strerror or exc}") from exc
    raise exc


def acquire(*, max_s: int, reason: str, clock: QuietClock | None = None) -> QuietOwner:
    """Take the lifetime lock, commit the cooldown, publish a fixed-deadline lease. No renewal.

    The caller must ``close()`` the owner (a ``finally``); process death releases the lock anyway.
    """
    max_s, reason = _validate_request(max_s, reason)
    clock = clock or SystemClock()
    if fcntl is None or not quiet_clock.lease_supported():
        raise QuietUnavailableError("claim-quiet lease needs POSIX flock and a suspend-inclusive clock")
    try:  # an unusable clock is refused before the filesystem is touched at all
        records.sample(clock)
    except ClockSampleError as exc:
        raise QuietUnavailableError(f"claim-quiet clock unusable: {exc}") from exc
    paths = quiet_paths()
    root_real = os.path.realpath(paths.root)
    try:
        owner = _open_owner(paths)
    except OSError as exc:
        raise QuietUnavailableError(f"claim-quiet directory/lock unusable: {exc.strerror}") from exc
    try:
        _start(owner, paths, root_real, clock, max_s, reason)
    except BaseException as exc:  # health: allow BLE001 -- cleanup runs, then the failure is re-raised
        _fail_start(owner, exc)
    return owner
