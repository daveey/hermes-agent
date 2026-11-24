"""Stable POSIX family fences shared by broker and exec producer child.

This is a custody primitive, NOT a legacy-process admission proof. Admission must
hold the launch gate and supply a strict COMPLETE census before acquiring it.
Never unlink the files and never LOCK_UN: an inherited child owns the same open
file description and must keep fencing successors after its broker disappears.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_FAMILIES = frozenset(('panel', 'links', 'annotation'))


class CustodyConflict(RuntimeError):
    """Another producer still holds actual kernel custody; expiry cannot steal it."""


class CustodyUnsafe(RuntimeError):
    """Runtime paths cannot establish private, stable lock custody."""


@dataclass
class FamilyCustody:
    fd: int | None

    @classmethod
    def acquire(cls, runtime: Path, family: str) -> FamilyCustody:
        """Nonblocking acquisition; caller deliberately pass_fds=(owner.fd,)."""
        if family not in _FAMILIES:
            raise ValueError('unknown producer family')
        return cls(_acquire_runtime(runtime, family))

    @classmethod
    def acquire_execution(cls, runtime: Path, family: str) -> FamilyCustody:
        """Prove previous execution retired, independently of broker custody.

        Only the producer opens this description. Descendants inherit it across
        fork/exec, so a broker's waitpid cannot admit overlapping replacement
        work. Keep the stable inode and never unlock a shared description.
        """
        if family not in _FAMILIES:
            raise ValueError('unknown producer family')
        fd = _acquire_runtime(runtime, f'{family}-execution')
        owner = cls(fd)
        try:
            os.set_inheritable(fd, True)
        except BaseException:
            owner.close()
            raise
        return owner

    def close(self) -> None:
        """Close only this process's descriptor, preserving inherited custody."""
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def _acquire_runtime(runtime: Path, name: str) -> int:
    """Shared stable-file mechanics for family fences and the admission gate."""
    try:
        import fcntl
    except ImportError as exc:
        raise CustodyUnsafe('no strict custody adapter for this platform') from exc
    runtime = Path(runtime)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CustodyUnsafe('private directory required') from exc
    try:
        _private(os.fstat(directory), directory=True)
        return _acquire_file(directory, name, fcntl)
    finally:
        os.close(directory)


def _private(info: os.stat_result, *, directory: bool) -> None:
    expected = 0o700 if directory else 0o600
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    links_ok = directory or info.st_nlink == 1
    uid = os.getuid()  # windows-footgun: ok — reached only after acquire imports the POSIX fcntl adapter
    if not kind_ok or not links_ok or info.st_uid != uid or stat.S_IMODE(info.st_mode) != expected:
        raise CustodyUnsafe('private directory required' if directory else 'private stable lock file required')


def _acquire_file(directory: int, family: str, fcntl) -> int:
    try:
        fd = os.open(f'{family}.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
    except OSError as exc:
        raise CustodyUnsafe('private stable lock file required') from exc
    try:
        _private(os.fstat(fd), directory=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CustodyConflict(f'{family} producer has not released custody') from exc
        return fd
    except BaseException:
        os.close(fd)
        raise
