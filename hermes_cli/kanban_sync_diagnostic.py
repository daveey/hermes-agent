"""Bounded quiet-window diagnostic refresh; no lock bypass or replay. [daveey]"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import threading
import time

from hermes_cli.kanban_sync_guard import board_sync_lock, SyncRoundBusy


@contextmanager
def fresh_cut(board, hub, gateway, path):
    """Hold the ordinary sync fence continuously from pull through caller audits.

    Release between behind chunks and on failed acquisition; never retry caller
    exceptions. Bounds admit new starts only; existing HTTP deadlines still apply.
    """
    from hermes_cli.kanban_sync_client import Pulled, _pulled_replica

    deadline = time.monotonic() + MAX_SECONDS
    result = Pulled(busy=True)
    for attempt in range(MAX_ATTEMPTS):
        with ExitStack() as stack:
            try:
                stack.enter_context(board_sync_lock(path))
            except SyncRoundBusy:
                result = Pulled(busy=True)
            else:
                with _pulled_replica(board, hub, gateway, diagnostic=True) as (_, result, _rebuilt):
                    if not result.more:
                        yield result
                        return
        remaining = deadline - time.monotonic()
        if attempt + 1 == MAX_ATTEMPTS or remaining <= 0:
            break
        _wait(min(RETRY_SECONDS, remaining))
        if time.monotonic() >= deadline:
            break
    yield result

MAX_ATTEMPTS = 12
MAX_SECONDS = 8.0
RETRY_SECONDS = 0.25


def _wait(seconds):
    threading.Event().wait(seconds)


def refresh(board, hub, gateway, path):
    """Retry busy/behind only, within both caps. Return incomplete on exhaustion.

    Each pull owns/releases its normal lock. HTTP errors and resync refusals are
    not quiet-window contention and propagate without retries. A pull already in
    progress retains its existing network deadline; the window bounds new starts.
    """
    from hermes_cli.kanban_sync_client import _pull_replica

    deadline = time.monotonic() + MAX_SECONDS
    for attempt in range(MAX_ATTEMPTS):
        result = _pull_replica(board, hub, gateway, path, diagnostic=True)
        if not result.busy and not result.more:
            return result
        remaining = deadline - time.monotonic()
        if attempt + 1 == MAX_ATTEMPTS or remaining <= 0:
            return result
        _wait(min(RETRY_SECONDS, remaining))
        if time.monotonic() >= deadline:
            return result
    return result
