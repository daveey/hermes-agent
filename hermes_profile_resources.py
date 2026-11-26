"""Cross-process custody of named-profile SQLite resources.

A lease is published *before* opening SQLite, outside the removable home. Deletion
first tombstones the name, then waits for every lease's kernel lock to be released.
A single process-local monitor drains canonical SessionDB owners on retirement;
checked-out readers retain their lease until their actual connection closes.
Lock files are unique, never reused, so a dead process is safely detected by the OS.
"""
from __future__ import annotations

import atexit
import logging
import os
import sqlite3
from pathlib import Path
import threading
import time
import uuid
import weakref

from hermes_constants import assert_named_profile_home_live, named_profile_home, named_profile_is_deleted

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_leases: set[ProfileResourceLease] = set()
_monitor: threading.Thread | None = None
_tick = threading.Event()
_probe_lock = threading.Lock()
_probe_owners = {}


def _lease_directory(home: Path) -> Path:
    return home.parent / '.resource-leases' / home.name


def _identity(home: Path) -> tuple[int, int]:
    stat = home.stat()
    return stat.st_dev, stat.st_ino


def _lease_path_matches(path: Path, handle) -> bool:
    try:
        current = path.stat()
    except FileNotFoundError:
        return False
    opened = os.fstat(handle.fileno())
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _unlink_lease(path: Path, handle) -> None:
    if not _lease_path_matches(path, handle):
        return
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        if os.name != 'nt':
            raise
        # Windows disallows unlinking an open CRT file. After closing, any other
        # open handle still prevents deletion; a pre-lock opener revalidates too.
        handle.close()
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            return  # Still open elsewhere; the next acquisition sweeps it.



class ProfileResourceLease:
    """One writer and its pooled readers; acknowledgement follows the last close."""

    def __init__(self, home: Path, *, drain_profile_probes: bool = False):
        from gateway.status import _try_acquire_file_lock

        self._drain_profile_probes = drain_profile_probes
        self.home = home
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._refs = 1
        self._owner_released = False
        self._retired = False
        self._retirement_pending = False
        self.ready = False
        self.owner = None
        self._pending_close: list[sqlite3.Connection] = []
        self.identity = _identity(home)
        directory = _lease_directory(home)
        directory.mkdir(parents=True, exist_ok=True)
        _resources_unowned(home)  # Reclaim unlocked residue, never another live owner.
        while True:
            self.path = directory / uuid.uuid4().hex
            self.handle = self.path.open('x+', encoding='utf-8')
            try:
                if not _try_acquire_file_lock(self.handle):
                    raise RuntimeError(f'Cannot lock resource lease for {home}')
                # A sweeper may have locked/unlinked our file before we locked it.
                # Do not open SQLite with an invisible lease (even if its name was replaced).
                if not _lease_path_matches(self.path, self.handle):
                    self.handle.close()
                    continue
                assert_named_profile_home_live(home)
                if _identity(home) != self.identity:
                    raise FileNotFoundError(f'Profile home changed during resource admission: {home}')
            except BaseException:
                _unlink_lease(self.path, self.handle)
                self.handle.close()
                raise
            break
        with _lock:
            _leases.add(self)
        try:
            _ensure_monitor()
        except BaseException:
            with _lock:
                _leases.discard(self)
            _unlink_lease(self.path, self.handle)
            self.handle.close()
            raise

    def retired(self) -> bool:
        if self._retired:
            return True
        try:
            self._retired = named_profile_is_deleted(self.home) or _identity(self.home) != self.identity
        except FileNotFoundError:
            self._retired = True
        return self._retired

    def set_owner(self, owner, *, retire=None) -> None:
        self.owner = weakref.ref(owner)
        self.retire_owner = retire
        self.ready = True

    def retain_reader(self) -> bool:
        with self._lock:
            if self._owner_released or self.retired():
                return False
            self._refs += 1
            return True

    def release_owner(self) -> None:
        with self._lock:
            if self._owner_released:
                return
            self._owner_released = True
        self.release_reader()

    def release_reader(self) -> None:
        with self._lock:
            self._refs -= 1
        _tick.set()

    def finish_if_drained(self) -> None:
        with self._finish_lock:
            self._finish_if_drained()

    def _finish_if_drained(self) -> None:
        self.retry_closes()
        with self._lock:
            if self._refs or self.handle.closed:
                return
        if self._drain_profile_probes:
            # Each cache publishes a dedicated probe owner BEFORE opening a raw fd.
            # Only that lease drains profile-wide probes. A closed SessionDB must not
            # retain a lease forever behind a different, still-live SQLite owner.
            from hermes_state_registry import _release_header_probes_under
            from hermes_state_dbfile import all_header_probes_under
            from hermes_cli.sqlite_safe_read import release_profile_pread_fds
            _release_header_probes_under(self.home)
            if all_header_probes_under(self.home):
                return
            if not release_profile_pread_fds(self.home):
                return
        _unlink_lease(self.path, self.handle)
        self.handle.close()  # the only positive acknowledgement is an actual close
        with _lock:
            _leases.discard(self)

    def close_connection(self, conn, *, reader: bool = False) -> None:
        try:
            conn.close()
        except Exception:  # health: allow BLE001 -- preserve the concrete failed-close resource for retry
            with self._lock:
                if not reader:
                    self._refs += 1
                self._pending_close.append(conn)
            logger.warning('Profile SQLite close failed for %s; retaining custody', self.home, exc_info=True)
            _tick.set()
        else:
            if reader:
                self.release_reader()

    def retry_closes(self) -> None:
        with self._lock:
            pending, self._pending_close = self._pending_close, []
        for conn in pending:
            self.close_connection(conn, reader=True)

    def assert_reopen_allowed(self) -> None:
        with self._lock:
            if self._owner_released or self.retired():
                raise FileNotFoundError(f'Profile resource owner is retired: {self.home}')


def lease_for_path(path: Path) -> ProfileResourceLease | None:
    home = named_profile_home(path.resolve())
    return None if home is None else ProfileResourceLease(home)


class _ProfileProbeOwner:
    def __init__(self, home: Path):
        self.lease = ProfileResourceLease(home, drain_profile_probes=True)
        self.lease.set_owner(self, retire=_retire_probe_owner)


def _retire_probe_owner(owner: _ProfileProbeOwner) -> None:
    with _probe_lock:
        if _probe_owners.get(owner.lease.home) is owner:
            del _probe_owners[owner.lease.home]
    owner.lease.release_owner()


def protect_profile_probes(path: Path) -> None:
    """Publish custody even for a process that only opens raw diagnostic probes."""
    home = named_profile_home(path.resolve())
    if home is None:
        return
    with _probe_lock:
        owner = _probe_owners.get(home)
        if owner is None:
            owner = _probe_owners[home] = _ProfileProbeOwner(home)
        owner.lease.assert_reopen_allowed()


def _ensure_monitor() -> None:
    global _monitor
    with _lock:
        if _monitor is None:
            monitor = threading.Thread(target=_monitor_resources, name='profile-resource-retirement', daemon=True)
            monitor.start()
            _monitor = monitor


def _monitor_resources() -> None:
    global _monitor
    while True:
        with _lock:
            if not _leases:
                _monitor = None
                return
            leases = list(_leases)
        for lease in leases:
            try:
                if lease.ready and lease.retired():
                    _start_retirement(lease)
            except OSError:
                logger.warning('Cannot inspect profile retirement for %s; retaining custody', lease.home, exc_info=True)
            except Exception:  # health: allow BLE001 -- retain leases on failed close, retry without acknowledging deletion
                logger.exception('Profile resource retirement failed for %s', lease.home)
        for lease in leases:
            try:
                lease.finish_if_drained()
            except (OSError, RuntimeError):
                logger.exception('Profile resource acknowledgement failed for %s', lease.home)
        _tick.wait(0.25)
        _tick.clear()


def _start_retirement(lease: ProfileResourceLease) -> None:
    # Slow unloads/queries must not prevent unrelated profiles from retiring.
    with lease._lock:
        if lease._owner_released or lease._retirement_pending:
            return
        lease._retirement_pending = True
    try:
        threading.Thread(target=_retire_owner, args=(lease,), name='profile-owner-retirement', daemon=True).start()
    except BaseException:
        with lease._lock:
            lease._retirement_pending = False
        raise


def _retire_owner(lease: ProfileResourceLease) -> None:
    from agent.runtime_scope import bind_profile_scope
    try:
        with bind_profile_scope(lease.home, hydrate_secrets=False):
            from hermes_state_registry import retire_resource_owner
            owner = lease.owner() if lease.owner is not None else None
            if owner is not None:
                (lease.retire_owner or retire_resource_owner)(owner)
    except Exception:  # health: allow BLE001 -- third-party unload failures retain custody for retry
        logger.exception('Profile owner retirement failed for %s', lease.home)
    finally:
        with lease._lock:
            lease._retirement_pending = False
        _tick.set()


def _resources_unowned(home: Path) -> bool:
    from gateway.status import _try_acquire_file_lock

    directory = _lease_directory(home)
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError:
        return True
    unowned = True
    for path in paths:
        try:
            handle = path.open('r+', encoding='utf-8')
        except FileNotFoundError:
            continue
        with handle:
            if _try_acquire_file_lock(handle):
                # Keep the lock through unlink. A pre-lock opener must revalidate
                # its inode after acquisition before it may open SQLite.
                _unlink_lease(path, handle)
            else:
                unowned = False
    return unowned


def _cleanup_drained_at_exit() -> None:
    # Daemon monitor may not get another tick after an explicit db.close().
    # Release diagnostic-only owners, but never acknowledge unclosed SQLite.
    with _probe_lock:
        probes = list(_probe_owners.values())
        _probe_owners.clear()
    for owner in probes:
        owner.lease.release_owner()
    with _lock:
        leases = list(_leases)
    for lease in leases:
        try:
            lease.finish_if_drained()
        except (OSError, RuntimeError):
            logger.exception('Cannot clean drained profile resource lease at exit')


atexit.register(_cleanup_drained_at_exit)


def wait_profile_resources(home: Path, *, timeout: float = 60.0) -> bool:
    """After tombstoning, wait for constructors, SQLite closes and active readers.

    Unreadable custody is an error, not an empty owner set. Never remove the lease
    directory: concurrent refused openers may still be finishing their cleanup.
    """
    if not named_profile_is_deleted(home):
        raise ValueError(f'Profile must be tombstoned before draining resources: {home}')
    deadline = time.monotonic() + timeout
    while not _resources_unowned(home):
        if time.monotonic() >= deadline:
            return False
        _tick.wait(min(0.05, max(0, deadline - time.monotonic())))
        _tick.clear()
    return True
