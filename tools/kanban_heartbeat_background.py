"""Observation-only automatic heartbeat: bounded single-flight, never on the turn thread.

Explicit heartbeat/lifecycle tools remain synchronous. The existing writer rechecks
run/claim/owner fences using the caller's copied context before extending anything.
"""
from agent.memory_provider import spawn_context_thread
import logging
import os
import threading
import time

_lock = threading.Lock()
_worker = None
logger = logging.getLogger(__name__)


def heartbeat_in_background():
    """Schedule at most one automatic heartbeat; return the thread for observers."""
    global _worker
    from tools import kanban_tools as kt
    if not os.environ.get('HERMES_KANBAN_TASK') or kt._is_delegated_child_context():
        return None
    with _lock:
        if _worker is not None and _worker.is_alive():
            return None
        if time.monotonic() - kt._auto_heartbeat_last_attempt < kt._AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
            return None

        def run():
            try:
                kt.heartbeat_current_worker_from_env()
            except Exception:
                logger.debug('automatic worker heartbeat failed', exc_info=True)

        try:
            _worker = spawn_context_thread(run, name='kanban-auto-heartbeat')
            _worker.start()
        except Exception:
            _worker = None
            # Failed admission also consumes the window: stream chunks must not
            # retry thread creation and emit a traceback on every activity touch.
            kt._auto_heartbeat_last_attempt = time.monotonic()
            logger.warning('automatic worker heartbeat could not start; no fallback admitted', exc_info=True)
            raise
        return _worker
