"""``fstat`` of many descriptors without a GIL hand-off per descriptor. [daveey]

``hermes_state_lockguard.hold`` runs on every guarded SQLite open and checks every descriptor the
process has open (hundreds in a long-lived dashboard). ``os.fstat`` releases and re-takes the GIL
around each call. With even one CPU-bound thread in the process, every re-take waits on the
interpreter's switch interval, so a scan that takes 0.8 ms idle took ~1.2 s. Kanban board opens hold
the board's lifecycle lock across that scan, which made every reader on the hub time out as
"board lifecycle is busy" in bursts across all boards.

``identity(fd)`` calls libc's ``fstat`` through ``ctypes.PyDLL``, which keeps the GIL for the call,
and reads ``(st_dev, st_ino)`` using a platform-specific verified layout: glibc on Linux
x86_64/aarch64, and Darwin on arm64/x86_64. Darwin's bounded proc_pidinfo listing avoids
per-entry GIL handoffs and rejects potentially truncated snapshots. Both paths must agree
with ``os.fstat`` at import; unsupported or unverified runtimes retain ``os.fstat``.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
from typing import Callable, Optional

Identity = tuple[int, int]

_UNPACK = struct.Struct("=QQ").unpack_from


def _os_identity(fd: int) -> Optional[Identity]:
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _build_native() -> Optional[tuple[Callable[[int], Optional[Identity]], Callable[[], Optional[bytes]]]]:
    if not sys.platform.startswith("linux") or platform.machine() not in ("x86_64", "aarch64"):
        return None
    try:
        import ctypes

        libc = ctypes.PyDLL(None)  # PyDLL: calls never release the GIL
        fstat = libc.fstat  # glibc >= 2.33 exports fstat itself
        fstat.argtypes = [ctypes.c_int, ctypes.c_void_p]
        fstat.restype = ctypes.c_int
        c_open, c_read, c_close = libc.open, libc.read, libc.close
        c_open.argtypes, c_open.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_int
        c_read.argtypes, c_read.restype = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_ssize_t
        c_close.argtypes, c_close.restype = [ctypes.c_int], ctypes.c_int
        buf = ctypes.create_string_buffer(256)  # sizeof(struct stat) is 144 (x86_64) / 128 (aarch64)
        status_buf = ctypes.create_string_buffer(16384)
    except (ImportError, OSError, AttributeError):  # no ctypes, no libc handle, or no fstat symbol
        return None

    def native(fd: int) -> Optional[Identity]:
        # One shared buffer: every caller holds hermes_state_lockguard._LOCK, and fstat() plus the
        # unpack is a single expression evaluation, so no other user of the buffer can interleave.
        if fstat(fd, buf) != 0:
            return None
        return _UNPACK(buf.raw[:16])

    def read_status() -> Optional[bytes]:
        fd = c_open(b"/proc/self/status", os.O_RDONLY | os.O_CLOEXEC)
        if fd < 0:
            return None
        try:
            n = c_read(fd, status_buf, len(status_buf))  # procfs serves status in one read
        finally:
            c_close(fd)
        return status_buf.raw[:n] if n > 0 else None

    # Prove the struct layout on this exact runtime before trusting it.
    probe = os.open(os.devnull, os.O_RDONLY)
    try:
        if native(probe) != _os_identity(probe) or native(-1) is not None or b"FDSize:" not in (read_status() or b""):
            return None
    except (OSError, ValueError, TypeError, struct.error, ctypes.ArgumentError):  # wrong ABI: disarm
        return None
    finally:
        os.close(probe)
    return native, read_status


_built = _build_native()
_native = _built[0] if _built else None
_read_status = _built[1] if _built else None
_darwin_descriptors = None
if sys.platform == "darwin":
    from hermes_state_lockguard_darwin import build as _build_darwin

    _darwin = _build_darwin()
    if _darwin is not None:
        _native, _darwin_descriptors = _darwin


def identity(fd: int) -> Optional[Identity]:
    """``(st_dev, st_ino)`` of ``fd``, or ``None`` when it is not open."""
    if _native is not None:
        return _native(fd)
    return _os_identity(fd)


def native_enabled() -> bool:
    return _native is not None


# Above this fd-table size a full native sweep would hold the GIL too long; use the listing instead.
MAX_NATIVE_SWEEP = 8192


def _fd_table_size() -> Optional[int]:
    """``FDSize`` from ``/proc/self/status``: the fd table's allocated size, > every open fd. Read
    through libc with the GIL held: Python's buffered ``open()`` makes several releasing syscalls."""
    status = _read_status() if _read_status is not None else None
    if not status:
        return None
    try:
        for line in status.splitlines():
            if line.startswith(b"FDSize:"):
                return int(line.split()[1])
    except (ValueError, IndexError):
        return None
    return None


def open_fd_identities() -> Optional[list[tuple[int, Identity]]]:
    """Open ``(fd, identity)`` pairs without a GIL handoff per syscall, or ``None``
    for the caller's platform directory-listing fallback. Linux sweeps below
    ``FDSize``; Darwin uses a bounded ``proc_pidinfo`` listing. Each native call
    holds the GIL, but the Python loop can still be scheduled between calls:
    this is not an atomic snapshot of descriptor opens/closes."""
    if _native is None:
        return None
    if _darwin_descriptors is not None:
        descriptors = _darwin_descriptors()
        if descriptors is None:
            return None
    else:
        size = _fd_table_size()
        if size is None or size <= 0 or size > MAX_NATIVE_SWEEP:
            return None
        descriptors = range(size)
    native = _native
    out = []
    for fd in descriptors:
        ident = native(fd)
        if ident is not None:
            out.append((fd, ident))
    return out
