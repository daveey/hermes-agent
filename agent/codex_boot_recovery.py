"""Keep exhausted Codex recovery off managed Kanban construction's critical path.

A boot never promotes an exhausted row from missing evidence. With a usable peer,
recovery runs on a separately loaded pool after construction. If selection raises
a typed Codex quota wall, construction restores ordinary inline recovery before
failing; daemon lifetime is never the only recovery path for an empty pool.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from contextvars import ContextVar, copy_context

_pending: ContextVar[list | None] = ContextVar('codex_boot_recovery', default=None)
_lock = threading.Lock()
_running: set[str] = set()


def defer_recovery() -> bool:
    pending = _pending.get()
    if pending is None:
        return False
    pending[:] = [True]
    return True


def resume_inline_recovery() -> None:
    """No usable credential: do not rely on a daemon surviving failed boot."""
    pending = _pending.get()
    if pending is not None:
        pending.clear()
    _pending.set(None)


def recovery_deferred() -> bool:
    return bool(_pending.get())


def _recover() -> None:
    from agent.credential_pool import load_pool
    pool = load_pool('openai-codex')
    # A fresh pool, not the constructing agent's pool/lock. The ordinary path
    # persists rotated tokens and merges clears against newer failure stamps.
    with pool._lock:
        pool._available_entries(clear_expired=True, refresh=False)


def _start(context) -> None:
    from hermes_cli.config import get_hermes_home
    home = str(get_hermes_home())
    with _lock:
        if home in _running:
            return
        _running.add(home)

    def run():
        try:
            context.run(_recover)
        except Exception:
            logging.getLogger(__name__).debug('Deferred Codex recovery failed', exc_info=True)
        finally:
            with _lock:
                _running.discard(home)

    from agent.memory_provider import spawn_context_thread
    try:
        spawn_context_thread(run, name='codex-boot-recovery', daemon=True).start()
    except Exception:
        with _lock:
            _running.discard(home)
        logging.getLogger(__name__).debug('Could not start deferred Codex recovery', exc_info=True)


@contextlib.contextmanager
def construction_scope(enabled: bool):
    if not enabled or _pending.get() is not None:
        yield
        return
    pending: list[bool] = []
    token = _pending.set(pending)
    try:
        yield
    finally:
        _pending.reset(token)
        if pending:
            # Preserve profile/route context, but not the deferral scope itself.
            _start(copy_context())
