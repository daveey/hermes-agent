"""Native annotation input notifications (no input polling). [daveey]"""
from __future__ import annotations

import ctypes
import errno
import os
import select
import struct
import sys
import threading
from pathlib import Path

_HEADER = struct.Struct('iIII')
_CHANGE = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
_LOST = 0x400 | 0x800 | 0x2000 | 0x8000
_OVERFLOW = 0x4000


class FileEvents:
    """Single-owner native source; only wake() may be called from another thread.

    Install coverage BEFORE reading a snapshot. On invalidation, reinstall before
    rereading: watches may have been lost or an absent ancestor created. Directory
    subscriptions cover immediate children; consumers must rediscover descendants.
    No polling fallback is permitted on platforms without a native adapter.
    """

    def __init__(self):
        if not sys.platform.startswith('linux'):
            raise NotImplementedError('annotation native notifications require Linux inotify')
        self._lib = ctypes.CDLL(None, use_errno=True)
        self._lib.inotify_init1.argtypes = [ctypes.c_int]
        self._lib.inotify_init1.restype = ctypes.c_int
        self._lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._lib.inotify_add_watch.restype = ctypes.c_int
        self._fd = self._open()
        try:
            self._wake_read, self._wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            os.close(self._fd)
            raise
        self._names: dict[int, set[str]] = {}
        self._directories: set[int] = set()
        self._controls: dict[int, set[str]] = {}
        self.control_only = False
        self._wake_lock = threading.Lock()

    def _open(self):
        fd = self._lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return fd

    def _install(self, fd, directory):
        wd = self._lib.inotify_add_watch(fd, os.fsencode(directory), _CHANGE)
        if wd < 0:
            code = ctypes.get_errno()
            if code in (errno.ENOENT, errno.ENOTDIR):
                return None
            raise OSError(code, os.strerror(code), str(directory))
        return wd

    def replace(self, files, *, directories=(), controls=()):
        """Atomically replace coverage; failure preserves the previous watches.

        A successful call requires a new snapshot, even if paths are unchanged:
        events queued on the previous descriptor are intentionally discarded.
        SQLite sidecars are watched even when absent at installation time.
        """
        names: dict[int, set[str]] = {}
        directory_wds: set[int] = set()
        # Keep lexical coverage for retargeting and resolved coverage for direct
        # writes/creation outside the alias tree, including dangling targets.
        def expanded(values):
            result = set()
            for value in values:
                path = Path(value).absolute()
                result.add(path)
                result.add(path.resolve(strict=False))
                for parent in path.parents:
                    result.add(parent.resolve(strict=False) / path.relative_to(parent))
            return result

        paths = expanded(files)
        control_paths = expanded(controls)
        control_names: dict[int, set[str]] = {}
        dirs = expanded(directories)
        fd = self._open()
        try:
            for path in [*paths, *dirs, *control_paths]:
                for child in reversed([path, *path.parents[:-1]]):
                    wd = self._install(fd, child.parent)
                    if wd is None:
                        break
                    target = control_names if child == path and path in control_paths else names
                    target.setdefault(wd, set()).add(child.name)
                    if child == path and path in paths:
                        names[wd].update(path.name + suffix for suffix in ('-wal', '-shm', '-journal'))
            for directory in dirs:
                wd = self._install(fd, directory)
                if wd is not None:
                    directory_wds.add(wd)
        except BaseException:
            os.close(fd)
            raise
        old, self._fd = self._fd, fd
        self._names, self._directories, self._controls = names, directory_wds, control_names
        os.close(old)

    def wake(self):
        """Interrupt a wait (e.g. demand, mutation or stop); no source I/O."""
        with self._wake_lock:
            if self._fd < 0:
                return
            try:
                os.write(self._wake_write, b'!')
            except BlockingIOError:
                pass  # A full pipe is already a pending wake.

    def _readable(self, timeout):
        # poll accepts valid descriptors above FD_SETSIZE, unlike select().
        waiter = select.poll()
        for fd in (self._fd, self._wake_read):
            waiter.register(fd, select.POLLIN)
        ready = waiter.poll(None if timeout is None else max(0, timeout * 1000))
        if any(mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL) for _, mask in ready):
            raise OSError(errno.EIO, 'annotation notification descriptor unavailable')
        return {fd for fd, _ in ready}

    def wait(self, timeout=None):
        readable = self._readable(timeout)
        changed = self._wake_read in readable
        self.control_only = not changed
        if changed:
            os.read(self._wake_read, 65536)
        if self._fd not in readable:
            return changed
        # Bound drain work under a continuous writer; remaining bytes stay ready.
        for _batch in range(16):
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                return changed
            if not data:
                raise OSError(errno.EIO, 'inotify closed during event read')
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, length = _HEADER.unpack_from(data, offset)
                offset += _HEADER.size
                name = os.fsdecode(data[offset:offset + length].split(b'\0', 1)[0])
                offset += length
                source = bool(mask & (_OVERFLOW | _LOST)) or wd in self._directories or (
                    name in self._names.get(wd, ())
                )
                control = name in self._controls.get(wd, ())
                changed |= source or control
                self.control_only &= not source
        return changed

    def close(self):
        """Close after the owner has left wait(); wake/join before closing."""
        with self._wake_lock:
            if self._fd >= 0:
                for fd in (self._fd, self._wake_read, self._wake_write):
                    os.close(fd)
                self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
