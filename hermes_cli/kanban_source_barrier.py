"""Permanent account flock for source custody.

One nonblocking POSIX lock beside future ``pins.sqlite3``. Runtime never
creates a missing lock. Explicit :func:`establish` may create the private
directory and file only before activation. This module is unactivated WIP
until remaining consumers exist.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading
from typing import Iterator

from hermes_cli import worker_compute_capacity as capacity
from hermes_cli.worker_account_boards import _refuse_test_registration

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX hosts provide flock
    fcntl = None

_LOCK_NAME = "pins.lock"
_CUSTODY_DIR = "source-custody"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_K1_FLAGS = (_O_NOFOLLOW | _O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOCTTY", 0))


class CustodyBusy(RuntimeError):
    """The account custody lock is held; the caller must not retry internally."""


class CustodyUnavailable(RuntimeError):
    """The account custody lock is missing, unreadable, or not a private lock file."""


_LIVE_HOLDS: dict[HeldCustody, tuple[int, int, Path]] = {}


class HeldCustody:
    """Open descriptors for a held account barrier. Release closes; it never unlinks."""

    def __init__(self, fd: int, dir_fd: int) -> None:
        self._fd = fd
        self._dir_fd = dir_fd

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        _LIVE_HOLDS.pop(self, None)
        fd, self._fd = self._fd, -1
        dir_fd, self._dir_fd = self._dir_fd, -1
        try:
            if fd >= 0:
                os.close(fd)
        finally:
            if dir_fd >= 0:
                os.close(dir_fd)


def lock_path() -> Path:
    """``policy_root()/source-custody/pins.lock``, beside future ``pins.sqlite3``."""
    return capacity.policy_root() / _CUSTODY_DIR / _LOCK_NAME


def _require_posix() -> None:
    if fcntl is None or os.name != "posix":
        raise CustodyUnavailable("source-custody lock requires POSIX flock")


def _close_quiet(fd: int, original: BaseException | None = None) -> None:
    try:
        os.close(fd)
    except OSError as close_exc:
        if original is not None:
            original.add_note(f"descriptor close also failed: {close_exc!r}")


def _stat_mode(st: os.stat_result, *, directory: bool) -> None:
    if directory:
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise CustodyUnavailable("source-custody directory is not a private directory")
        expected = _DIR_MODE
    else:
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise CustodyUnavailable("source-custody lock is not a private regular file")
        expected = _FILE_MODE
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != expected:  # windows-footgun: ok (POSIX-only barrier)
        raise CustodyUnavailable("source-custody path is not private to this user")


def _same_inode(held: os.stat_result, at_path: os.stat_result) -> None:
    if (held.st_dev, held.st_ino) != (at_path.st_dev, at_path.st_ino):
        raise CustodyUnavailable("source-custody lock inode was replaced")


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise CustodyUnavailable("source-custody lock is unavailable") from exc


def open_directory(path: Path) -> int:
    """Walk lexical absolute parents without following any component symlink."""
    absolute = path if path.is_absolute() else Path.cwd() / path
    if '..' in absolute.parts:
        raise OSError('custody parent traversal is uncertain')
    flags = os.O_RDONLY | os.O_DIRECTORY | _K1_FLAGS
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(name: str | Path, flags: int = os.O_RDONLY, *, dir_fd: int | None = None,
                 mode: int = 0o600) -> int:
    """K1 open: nonblocking and non-following, with a regular-file fd result.

    A supplied dir_fd is already held by the canonical directory walk. Otherwise
    this helper performs that walk. Ownership/mode rules remain with callers.
    """
    parent = -1
    fd = -1
    try:
        if dir_fd is None:
            path = Path(name)
            parent = open_directory(path.parent)
            dir_fd, name = parent, path.name
        elif Path(name).name != str(name) or str(name) in {'.', '..'}:
            raise ValueError('custody relative input must be one component')
        flags |= _K1_FLAGS
        fd = os.open(name, flags, mode, dir_fd=dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('custody input is not a regular file')
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        if parent >= 0:
            os.close(parent)


def read_regular(name: str | Path, *, max_bytes: int, dir_fd: int | None = None,
                 validate=None) -> bytes:
    """Read at most cap+1, refusing oversize input and closing every descriptor."""
    if max_bytes < 0:
        raise ValueError('negative custody read limit')
    fd = open_regular(name, dir_fd=dir_fd)
    try:
        if validate is not None:
            validate(os.fstat(fd))
        if os.fstat(fd).st_size > max_bytes:
            raise ValueError('custody input exceeds read limit')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise ValueError('custody input exceeds read limit')
        return b''.join(chunks)
    finally:
        os.close(fd)


def _open_policy_root(root: Path) -> int:
    try:
        fd = open_directory(root)
    except OSError as exc:
        raise CustodyUnavailable("source-custody policy root is unavailable") from exc
    try:
        held = os.fstat(fd)
        if not stat.S_ISDIR(held.st_mode) or stat.S_ISLNK(held.st_mode):
            raise CustodyUnavailable("source-custody policy root is not a directory")
        if held.st_uid != os.geteuid():  # windows-footgun: ok (POSIX-only barrier)
            raise CustodyUnavailable("source-custody policy root is not private to this user")
        _same_inode(held, os.lstat(root))
    except OSError as exc:
        _close_quiet(fd, exc)
        raise CustodyUnavailable("source-custody policy root is unavailable") from exc
    except BaseException as exc:
        _close_quiet(fd, exc)
        raise
    return fd


def _open_dir(root_fd: int, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(_CUSTODY_DIR, _DIR_MODE, dir_fd=root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CustodyUnavailable("source-custody lock is unavailable") from exc
    try:
        fd = os.open(
            _CUSTODY_DIR, os.O_RDONLY | os.O_DIRECTORY | _K1_FLAGS, dir_fd=root_fd,
        )
    except OSError as exc:
        raise CustodyUnavailable("source-custody directory is unavailable") from exc
    try:
        held = os.fstat(fd)
        _stat_mode(held, directory=True)
        at_path = os.stat(_CUSTODY_DIR, dir_fd=root_fd, follow_symlinks=False)
        _same_inode(held, at_path)
    except OSError as exc:
        _close_quiet(fd, exc)
        raise CustodyUnavailable("source-custody directory is unavailable") from exc
    except BaseException as exc:
        _close_quiet(fd, exc)
        raise
    return fd


def _open_lock(dir_fd: int, *, create: bool) -> int:
    flags = os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC
    fd = -1
    created = False
    try:
        if create:
            try:
                fd = open_regular(_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL,
                                  mode=_FILE_MODE, dir_fd=dir_fd)
                created = True
            except FileExistsError:
                fd = open_regular(_LOCK_NAME, flags, dir_fd=dir_fd)
        else:
            fd = open_regular(_LOCK_NAME, flags, dir_fd=dir_fd)
        if created:
            os.fchmod(fd, _FILE_MODE)
        held = os.fstat(fd)
        _stat_mode(held, directory=False)
        at_path = os.stat(_LOCK_NAME, dir_fd=dir_fd, follow_symlinks=False)
        _same_inode(held, at_path)
    except OSError as exc:
        if fd >= 0:
            _close_quiet(fd, exc)
        raise CustodyUnavailable("source-custody lock is unavailable") from exc
    except BaseException as exc:
        if fd >= 0:
            _close_quiet(fd, exc)
        raise
    return fd


def _activation_marker() -> Path:
    return capacity.policy_root() / "source-custody.activation.json"


def _refuse_if_activated() -> None:
    try:
        os.lstat(_activation_marker())
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CustodyUnavailable("source-custody lock is unavailable") from exc
    raise CustodyUnavailable("source-custody lock setup is closed after activation")


def establish() -> Path:
    """Create the private lock directory and file before activation only.

    Does not create ``pins.sqlite3`` or the activation marker. Never unlinks.
    """
    _require_posix()
    _refuse_test_registration()
    _refuse_if_activated()
    path = lock_path()
    root = capacity.policy_root()
    root_fd = _open_policy_root(root)
    try:
        dir_fd = _open_dir(root_fd, create=True)
        try:
            fd = _open_lock(dir_fd, create=True)
            try:
                _fsync(fd)
                _fsync(dir_fd)
                _fsync(root_fd)
            except BaseException as exc:
                _close_quiet(fd, exc)
                raise
            os.close(fd)
        except BaseException as exc:
            _close_quiet(dir_fd, exc)
            raise
        else:
            os.close(dir_fd)
    except BaseException as exc:
        _close_quiet(root_fd, exc)
        raise
    else:
        os.close(root_fd)
    with hold():
        _refuse_if_activated()
    return path


def _flock_exclusive(fd: int) -> None:
    if fcntl is None:
        raise CustodyUnavailable("source-custody lock requires POSIX flock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise CustodyBusy("source-custody lock is busy") from exc
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            raise CustodyBusy("source-custody lock is busy") from exc
        raise CustodyUnavailable("source-custody lock is unavailable") from exc


def _revalidate(dir_fd: int, fd: int, root_fd: int) -> None:
    try:
        _same_inode(os.fstat(root_fd), os.lstat(capacity.policy_root()))
        dir_held = os.fstat(dir_fd)
        _stat_mode(dir_held, directory=True)
        at_dir = os.stat(_CUSTODY_DIR, dir_fd=root_fd, follow_symlinks=False)
        _same_inode(dir_held, at_dir)
        held = os.fstat(fd)
        _stat_mode(held, directory=False)
        at_path = os.stat(_LOCK_NAME, dir_fd=dir_fd, follow_symlinks=False)
        _same_inode(held, at_path)
    except OSError as exc:
        raise CustodyUnavailable("source-custody lock is unavailable") from exc


def validate_held(held: HeldCustody) -> None:
    """Accept only this thread's live context, never arbitrary open descriptors."""
    if type(held) is not HeldCustody or _LIVE_HOLDS.get(held) != (
            os.getpid(), threading.get_ident(), capacity.policy_root()):
        raise CustodyUnavailable("invalid, expired or foreign custody barrier")
    root_fd = _open_policy_root(capacity.policy_root())
    try:
        _revalidate(held._dir_fd, held._fd, root_fd)
    finally:
        os.close(root_fd)


@contextmanager
def hold() -> Iterator[HeldCustody]:
    """Acquire the existing account lock without creating or unlinking it."""
    _require_posix()
    root_fd = _open_policy_root(capacity.policy_root())
    dir_fd = -1
    fd = -1
    try:
        dir_fd = _open_dir(root_fd, create=False)
        fd = _open_lock(dir_fd, create=False)
        _revalidate(dir_fd, fd, root_fd)
        _flock_exclusive(fd)
        _revalidate(dir_fd, fd, root_fd)
    except BaseException as exc:
        if fd >= 0:
            _close_quiet(fd, exc)
            fd = -1
        if dir_fd >= 0:
            _close_quiet(dir_fd, exc)
            dir_fd = -1
        _close_quiet(root_fd, exc)
        raise
    os.close(root_fd)
    held = HeldCustody(fd, dir_fd)
    _LIVE_HOLDS[held] = (os.getpid(), threading.get_ident(), capacity.policy_root())
    try:
        yield held
    finally:
        held.close()
