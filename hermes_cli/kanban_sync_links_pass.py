"""The sync loop's chat-link pass, on its own thread so it can never hold up a sync round. [daveey]

Inline in ``kanban_sync_client._run_forever``, a slow pass held the replica pull, the remote-parent mirror and the
outbox push for its whole length: on the hub 2026-11-13 one pass took ~10 min (04:14-04:24Z, its strict ledger read
queued on ``_links_lock`` behind Desktop readers) and the replica went 13 min stale, so hub cards waiting on worker
cards did not promote (t_01a1d2cc49634a78). The loop asks for a pass on its cadence; a request while one is running
is refused and asked again on a later iteration, so passes never pile up. The thread owns its own connection scope
and never calls the loop's ``keep_running`` (callers count its calls); the loop stops it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hermes_cli.kanban_sync_client")


class LinksPass:
    JOIN_S = 5.0

    def __init__(self, gateway: str) -> None:
        self._gateway = gateway
        self._want = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stopped = False
        self._boards: list[tuple[str, Path]] = []
        self._thread: Optional[threading.Thread] = None

    def request(self, boards: list[tuple[str, Path]]) -> bool:
        """Start a pass over ``boards`` unless one is running. True when it started."""
        if not self._idle.is_set():
            return False
        self._boards = list(boards)
        self._idle.clear()
        self._want.set()
        if self._thread is None or not self._thread.is_alive():
            from agent.memory_provider import spawn_context_thread
            self._thread = spawn_context_thread(self._run, name="kanban-sync-links")
            self._thread.start()
        return True

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def stop(self) -> None:
        self._stopped = True
        self._want.set()
        if self._thread is not None:
            self._thread.join(self.JOIN_S)

    def _run(self) -> None:
        from hermes_cli import kanban_sync_client as client  # module attribute: tests replace reconcile_links
        from hermes_cli.kanban_sync_connections import connection_scope
        with connection_scope():
            try:
                while not self._stopped:
                    if not self._want.wait(timeout=1.0):
                        continue
                    self._want.clear()
                    if self._stopped:
                        break
                    try:
                        client.reconcile_links(self._boards, self._gateway, skip_unchanged=True)
                    except Exception as exc:  # health: allow BLE001 -- a failed pass must not end the thread
                        _log.warning("kanban sync: chat links pass failed (%s: %s)", type(exc).__name__, exc)
                    finally:
                        self._idle.set()
            finally:
                self._idle.set()
