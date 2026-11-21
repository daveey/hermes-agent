"""Bounded background exact-head CI observations; never wait inside the dispatch lock."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import RLock
import time

TTL_SECONDS = 300
MAX_ENTRIES = 256
OBSERVATION_BUDGET_SECONDS = 120
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kanban-idle-pr")
_cache: OrderedDict[tuple, tuple[float, bool]] = OrderedDict()
_pending: dict[tuple, tuple[float, Future]] = {}
_lock = RLock()


def clear_cache() -> None:
    with _lock:
        _clear_cache()


def _clear_cache() -> None:
    _cache.clear()
    # Running jobs have no DB handle and cannot publish a release themselves.
    for _started, future in _pending.values():
        future.cancel()
    _pending.clear()


def _collect(contract, assignee, head) -> bool:
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    from hermes_cli.kanban_pr_acceptance import collect_acceptance
    receipt = collect_acceptance(contract, contract, assignee=assignee)
    green = (receipt.get("ok") is True and receipt.get("pr_state") == "OPEN"
             and receipt.get("pr_url") == contract and receipt.get("head_sha") == head
             and bool(receipt.get("checks")))
    return bool(green and prs.pr_head(contract, max_age=0) == head)


def green(key, contract, assignee, head) -> bool:
    with _lock:
        return _green(key, contract, assignee, head)


def _green(key, contract, assignee, head) -> bool:
    """At most two outstanding jobs, no queued backlog, discard over-budget observations."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now < hit[0]:
        return hit[1]
    # Reap finished jobs even if their original task never asks again.
    for old, (started, future) in list(_pending.items()):
        if future.done():
            try:
                passed = future.result() is True and now - started <= OBSERVATION_BUDGET_SECONDS
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                # Failed observation remains fail-closed; never retain exception text.
                passed = False
            _cache[old] = (now + TTL_SECONDS, passed)
            _pending.pop(old)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)
    hit = _cache.get(key)
    if hit is not None and now < hit[0]:
        return hit[1]
    if key not in _pending and len(_pending) < 2:
        _pending[key] = (now, _pool.submit(copy_context().run, _collect, contract, assignee, head))
        if _pending[key][1].done():
            return green(key, contract, assignee, head)
    return False  # next tick consumes the result; the 35-min home cap bounds an outage
