"""Verified Darwin fd identities without per-syscall GIL handoffs. [daveey]

Darwin sys/stat.h: dev_t at offset 0, ino64_t at offset 8. proc_info.h:
PROC_PIDLISTFDS=1 returns proc_fdinfo {int32 fd; uint32 type}. A full buffer
may be truncated: reject it, never hand the WAL guard a partial snapshot.
"""
from __future__ import annotations

import os
import platform
import struct
import sys

MAX_FDS = 8192


def build():
    if sys.platform != "darwin" or platform.machine() not in ("arm64", "x86_64"):
        return None
    try:
        import ctypes

        libc = ctypes.PyDLL(None)
        fstat = getattr(libc, "fstat$INODE64", None) or libc.fstat
        fstat.argtypes, fstat.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_int
        pidinfo = libc.proc_pidinfo
        pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        pidinfo.restype = ctypes.c_int
    except (ImportError, OSError, AttributeError):
        return None

    mask = (1 << 64) - 1
    # CPython builds differ in how a negative int32 dev_t becomes st_dev
    # (signed vs uint64). None = not yet proven on this interpreter.
    unsigned_dev = [None]

    def learn(fd, dev, ino):
        # Until a negative device has been observed, ask Python itself (one
        # GIL-releasing fstat) and learn its conversion only from a consistent
        # answer. Python's own st_dev is always the authoritative identity.
        try:
            st = os.fstat(fd)
        except OSError:
            return None
        if st.st_ino == ino and st.st_dev in (dev, dev & mask):
            unsigned_dev[0] = st.st_dev != dev
        return st.st_dev, st.st_ino

    def raw(fd):
        # Per-call buffers also make independent diagnostic callers thread-safe.
        buf = ctypes.create_string_buffer(256)
        if fd < 0 or fd > 2147483647 or fstat(fd, buf) != 0:
            return None
        dev, _mode, _links, ino = struct.unpack_from("=iHHQ", buf)
        return dev, ino

    def identity(fd):
        got = raw(fd)
        if got is None:
            return None
        dev, ino = got
        if dev >= 0:
            return dev, ino
        if unsigned_dev[0] is None:
            return learn(fd, dev, ino)
        return (dev & mask if unsigned_dev[0] else dev), ino

    def descriptors():
        buf = ctypes.create_string_buffer(MAX_FDS * 8)
        size = pidinfo(os.getpid(), 1, 0, buf, len(buf))
        if size <= 0 or size >= len(buf) or size % 8:
            return None
        return [fd for fd, _kind in struct.iter_unpack("=iI", buf.raw[:size])]

    try:
        probe = os.open(os.devnull, os.O_RDONLY)
    except OSError:
        return None
    try:
        st = os.fstat(probe)
        # A negative /dev/null device proves the conversion now; a nonnegative
        # one (devfs numbering varies by host) leaves it to learn() at runtime.
        # Either way the native identity must match Python's exactly.
        if identity(probe) != (st.st_dev, st.st_ino) or identity(-1) is not None:
            return None
        if identity(probe) != (st.st_dev, st.st_ino):
            return None
        # The raw native device must itself agree with Python's st_dev, and a
        # negative sentinel must have taught the conversion.
        native_dev = raw(probe)[0]
        if native_dev < 0 and unsigned_dev[0] is None:
            return None
        if native_dev >= 0 and native_dev != st.st_dev:
            return None
        listed = descriptors()
        if listed is None or probe not in listed:
            return None
    except (OSError, ValueError, TypeError, struct.error, ctypes.ArgumentError):
        return None
    finally:
        os.close(probe)
    return identity, descriptors
