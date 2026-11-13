"""[daveey] Debug tripwire: log the stack when an ``ssl.SSLSocket`` is closed while another thread reads it.

``close()`` from a thread other than the one inside ``recv`` releases the fd under the reader's live SSL
object; the kernel recycles the number and the reader's next ``SSL_read`` / alert write lands in whatever
file took it (hub SQLite headers, 2026-11-11 and 2026-11-13). The FD-safe stranger-thread wake is
``shutdown()`` (``agent_runtime_helpers._shutdown_socket``). Set ``HERMES_DEBUG_SSL_CLOSE=1`` to install;
off by default because every TLS read then pays a Python-level wrapper.
"""

from __future__ import annotations

import logging
import os
import ssl
import threading

logger = logging.getLogger(__name__)

_ENV = "HERMES_DEBUG_SSL_CLOSE"
_READS = ("recv", "recv_into", "read")
_installed = False
_install_lock = threading.Lock()


def _wrap_read(original):
    def read(self, *args, **kwargs):
        readers = self.__dict__.setdefault("_hermes_readers", {})
        tid = threading.get_ident()
        readers[tid] = readers.get(tid, 0) + 1
        try:
            return original(self, *args, **kwargs)
        finally:
            if readers.get(tid, 0) <= 1:
                readers.pop(tid, None)
            else:
                readers[tid] -= 1
    read.__wrapped__ = original
    return read


def _wrap_close(original):
    def _real_close(self, *args, **kwargs):
        me = threading.get_ident()
        others = [tid for tid in dict(self.__dict__.get("_hermes_readers") or {}) if tid != me]
        if others:
            logger.warning("SSL socket fd=%s closed by thread %s while thread(s) %s read it: the fd can be recycled "
                           "under their SSL object (use shutdown() from a stranger thread)",
                           _fileno(self), threading.current_thread().name, others, stack_info=True)
        return original(self, *args, **kwargs)
    _real_close.__wrapped__ = original
    return _real_close


def _fileno(sock) -> int:
    try:
        return sock.fileno()
    except OSError:
        return -1


def install() -> bool:
    """Patch ``ssl.SSLSocket`` once; True when this call installed it."""
    global _installed
    with _install_lock:
        if _installed:
            return False
        for name in _READS:
            setattr(ssl.SSLSocket, name, _wrap_read(getattr(ssl.SSLSocket, name)))
        ssl.SSLSocket._real_close = _wrap_close(ssl.SSLSocket._real_close)
        _installed = True
        return True


def uninstall() -> None:
    """Restore the originals (tests)."""
    global _installed
    with _install_lock:
        if not _installed:
            return
        for name in (*_READS, "_real_close"):
            setattr(ssl.SSLSocket, name, getattr(ssl.SSLSocket, name).__wrapped__)
        _installed = False


def install_from_env() -> bool:
    if os.environ.get(_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return install()
