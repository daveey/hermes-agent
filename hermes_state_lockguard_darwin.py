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

    unsigned_dev = False

    def identity(fd):
        # Per-call buffers also make independent diagnostic callers thread-safe.
        buf = ctypes.create_string_buffer(256)
        if fd < 0 or fd > 2147483647 or fstat(fd, buf) != 0:
            return None
        dev, _mode, _links, ino = struct.unpack_from("=iHHQ", buf)
        return (dev & ((1 << 64) - 1) if unsigned_dev else dev), ino

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
        raw = identity(probe)
        # CPython builds differ in signed dev_t conversion. Require a negative
        # native device sentinel so the build's conversion is actually proven.
        if raw is None or raw[0] >= 0:
            return None
        unsigned_dev = st.st_dev == (raw[0] & ((1 << 64) - 1))
        if identity(probe) != (st.st_dev, st.st_ino) or identity(-1) is not None:
            return None
        listed = descriptors()
        if listed is None or probe not in listed:
            return None
    except (OSError, ValueError, TypeError, struct.error, ctypes.ArgumentError):
        return None
    finally:
        os.close(probe)
    return identity, descriptors
