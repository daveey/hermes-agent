"""A Linux stand-in for ``tools.credential_files_windows.Win32Handles``.

It mirrors the Win32 semantics the pinned walk relies on, with one exception:

* Ancestors are followed (``CreateFileW`` follows reparse points above the last component).
* The last component is opened as itself (``FILE_FLAG_OPEN_REPARSE_POINT``). A symlink comes
  back as an ``O_PATH`` fd whose ``fstat`` reports ``S_IFLNK``.
* ``final_path`` reads ``/proc/self/fd`` (``GetFinalPathNameByHandleW``).

The exception: Linux has no share modes, so a held fd does **not** stop a rename. Tests that
use this emulation therefore exercise the walk's reparse, identity and final-path checks
without the pinning. Real pinning is covered only by the Windows-only tests.
"""

from __future__ import annotations

import os
import sys

AVAILABLE = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd") and hasattr(os, "O_PATH")


class LinuxPinnedHandles:
    def __init__(self) -> None:
        self._opened: dict[int, tuple[str, tuple[int, int]]] = {}  # fd -> (path, identity)
        self.file_opens: list[tuple[str, set[str]]] = []  # (file path, paths still held then)

    def open_pinned(self, path: str, directory: bool) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0)
        if not directory:
            self.file_opens.append((path, set(self.held())))
        try:
            fd = os.open(path, flags)
        except OSError:
            if not os.path.islink(path):
                raise
            fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)  # the reparse point itself
        st = os.fstat(fd)
        self._opened[fd] = (path, (st.st_dev, st.st_ino))
        return fd

    def final_path(self, fd: int) -> str:
        return os.readlink(f"/proc/self/fd/{fd}")

    def held(self) -> list[str]:
        """Paths opened through this API whose fd is still open (on the same object)."""
        out = []
        for fd, (path, ident) in self._opened.items():
            try:
                st = os.fstat(fd)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == ident:
                out.append(path)
        return out
