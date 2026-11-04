"""Race-free reads of a skills tree on Windows, for the sanitized sandbox copy.

Windows has no ``O_NOFOLLOW`` and no ``dir_fd`` opens, so the POSIX walk in
:mod:`tools.credential_files` can't run there. A plain path copy is not safe either: the
source may be writable by the sandbox that mounts the copy, and a file or any directory
above it could be swapped for a junction or symlink to a host path between "vetted" and
"read".

This walk pins the path instead of anchoring it:

* Every directory from the drive root down to the directory being listed, and each file
  while it is read, is held open **without** ``FILE_SHARE_DELETE``. A rename or delete
  needs ``DELETE`` access to the object, so nothing on that chain can be renamed away or
  replaced until the handle closes. The handles request data access (list/read), because
  NT records a share mode only for opens that ask for data or delete access. An
  attributes-only open would not block anything.
* Each component is opened with ``FILE_FLAG_OPEN_REPARSE_POINT``, so a junction or symlink
  that replaced it is opened as itself and refused, never followed.
* Before a handle is trusted, it must show no reparse attribute and the expected type, the
  same identity (``st_dev``, ``st_ino``) as the entry that was listed and vetted, and a final
  path (``GetFinalPathNameByHandleW``) equal to the expected one.

The final-path check alone does not make this safe. A directory could still be swapped
after its check and before a child is opened. The non-delete-sharing handles held through
the read are what close that window. The final-path and identity checks make sure each
pinned handle is the object that was vetted.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Callable, Collection, Iterator, Optional, Protocol

logger = logging.getLogger(__name__)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# (rel, open_file(name, listed) -> fd, [(name, listed_stat), ...]); see walk_tree.
TreeItem = tuple[str, Callable[[str, os.stat_result], int], list[tuple[str, os.stat_result]]]


class HandleApi(Protocol):
    def open_pinned(self, path: str, directory: bool) -> int:
        """Open *path* itself, without following a final reparse point and without delete
        sharing; return a CRT fd that owns the handle."""
        ...

    def final_path(self, fd: int) -> str:
        """The final path of the object behind *fd*."""
        ...


class Win32Handles:
    """ctypes binding for :class:`HandleApi` (Windows only)."""

    _GENERIC_READ = 0x80000000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    # Deliberately no FILE_SHARE_DELETE (0x4): renaming or deleting a held object fails.
    _SHARE_READ_WRITE = 0x1 | 0x2
    _OPEN_EXISTING = 3
    _FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a directory handle

    def __init__(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self._ctypes = ctypes
        self._msvcrt = msvcrt
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create = kernel32.CreateFileW
        self._create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._create.restype = wintypes.HANDLE
        self._final = kernel32.GetFinalPathNameByHandleW
        self._final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self._final.restype = wintypes.DWORD
        self._close = kernel32.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL

    def _error(self, path: str) -> OSError:
        code = self._ctypes.get_last_error()
        return OSError(None, self._ctypes.FormatError(code).strip(), path, code)

    def open_pinned(self, path: str, directory: bool) -> int:
        if directory:
            access = self._FILE_LIST_DIRECTORY | self._FILE_READ_ATTRIBUTES | self._SYNCHRONIZE
            flags = self._FLAG_OPEN_REPARSE_POINT | self._FLAG_BACKUP_SEMANTICS
        else:
            access, flags = self._GENERIC_READ, self._FLAG_OPEN_REPARSE_POINT
        handle = self._create(path, access, self._SHARE_READ_WRITE, None, self._OPEN_EXISTING, flags, None)
        if handle is None or handle == self._ctypes.c_void_p(-1).value:
            raise self._error(path)
        try:
            return self._msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except OSError:
            self._close(handle)
            raise

    def final_path(self, fd: int) -> str:
        buf = self._ctypes.create_unicode_buffer(32768)
        n = self._final(self._msvcrt.get_osfhandle(fd), buf, len(buf), 0)  # FILE_NAME_NORMALIZED|VOLUME_NAME_DOS
        if not n or n >= len(buf):
            raise self._error(f"<fd {fd}>")
        return buf.value


def load_api() -> Optional[HandleApi]:
    """The Win32 binding on Windows; ``None`` elsewhere or when it can't be loaded."""
    if os.name != "nt":
        return None
    try:
        return Win32Handles()
    except (ImportError, OSError, AttributeError):
        logger.exception("credential_files: Win32 handle API unavailable; skills copies will be withheld")
        return None


def _is_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _plain(path: str) -> str:
    """Drop the ``\\\\?\\`` / ``\\\\?\\UNC\\`` prefix ``GetFinalPathNameByHandleW`` adds, then normcase."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(path)


def open_verified(api: HandleApi, path: str, *, directory: bool,
                  listed: Optional[os.stat_result] = None) -> int:
    """Open *path* pinned and return its fd only if the handle is the vetted, non-reparse
    object at exactly *path*; otherwise close it and raise ``OSError``."""
    fd = api.open_pinned(path, directory)
    try:
        st = os.fstat(fd)
        if _is_reparse(st):
            raise OSError(f"reparse point: {path}")
        if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
            raise OSError(f"not a {'directory' if directory else 'regular file'}: {path}")
        if listed is not None and (st.st_dev, st.st_ino) != (listed.st_dev, listed.st_ino):
            raise OSError(f"replaced since it was listed: {path}")
        final = api.final_path(fd)
        if _plain(final) != _plain(path):
            raise OSError(f"opens {final}, not {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def pin_path(api: HandleApi, real_dir: str, held: list[int]) -> None:
    """Open every component of the resolved *real_dir* from its drive root, appending each
    verified fd to *held*. The caller closes them. A component that is now a reparse point
    or another object raises ``OSError``."""
    drive, tail = os.path.splitdrive(real_dir)
    current = drive + os.sep
    held.append(open_verified(api, current, directory=True))
    for part in tail.split(os.sep):
        if part:
            current = os.path.join(current, part)
            held.append(open_verified(api, current, directory=True))


def walk_tree(api: HandleApi, root: Path, denied: Callable[[Path], bool], excluded: Collection[str],
              skip_id: Optional[tuple[int, int]]) -> Iterator[TreeItem]:
    """Yield ``(rel, open_file, files)`` for the resolved *root*, like the POSIX anchored walk.

    *rel* is ``""`` or ``os.sep``-joined (``"\\\\a\\\\b"``), so ``dst_root + rel`` matches the
    destination's ``os.walk`` spelling. ``open_file(name, listed)`` returns a verified fd for
    a listed file. It is valid only until the next item, while this directory and all its
    ancestors are still pinned. Fails closed: a root that can't be pinned yields nothing.
    """
    held: list[int] = []
    try:
        try:
            pin_path(api, str(root), held)
        except OSError as exc:
            logger.warning("credential_files: withholding skills root %s: cannot pin it without reparse points (%s)",
                           root, exc)
            return
        yield from _visit(api, root, str(root), "", denied, excluded, skip_id)
    finally:
        for fd in reversed(held):
            os.close(fd)


def _visit(api: HandleApi, root: Path, dir_path: str, rel: str, denied: Callable[[Path], bool],
           excluded: Collection[str], skip_id: Optional[tuple[int, int]]) -> Iterator[TreeItem]:
    base = Path(str(root) + rel)
    files: list[tuple[str, os.stat_result]] = []
    subdirs: list[tuple[str, os.stat_result]] = []
    with os.scandir(dir_path) as entries:  # dir_path and every ancestor are pinned by the caller
        for entry in entries:
            try:
                st = os.lstat(os.path.join(dir_path, entry.name))  # identity before vetting: what is vetted gets opened
            except OSError:
                continue
            if _is_reparse(st):
                continue
            if stat.S_ISDIR(st.st_mode):
                if (entry.name not in excluded and (st.st_dev, st.st_ino) != skip_id
                        and not denied(base / entry.name)):
                    subdirs.append((entry.name, st))
            elif stat.S_ISREG(st.st_mode) and not denied(base / entry.name):
                files.append((entry.name, st))

    def open_file(name: str, listed: os.stat_result) -> int:
        return open_verified(api, os.path.join(dir_path, name), directory=False, listed=listed)

    yield rel, open_file, sorted(files, key=lambda item: item[0])
    for name, listed in sorted(subdirs, key=lambda item: item[0]):
        child = os.path.join(dir_path, name)
        try:
            fd = open_verified(api, child, directory=True, listed=listed)
        except OSError as exc:  # now a junction/symlink/file, gone, or another directory
            logger.warning("credential_files: skipping skills dir %s: %s", base / name, exc)
            continue
        try:
            yield from _visit(api, root, child, f"{rel}{os.sep}{name}", denied, excluded, skip_id)
        finally:
            os.close(fd)
