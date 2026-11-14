"""Shared absolute read deadline for CI discovery and metadata downloads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import sys
import time
import urllib.request

# Direct CLI execution starts with scripts/ci on sys.path, not the repository root.
try:
    from hermes_cli.kanban_sync_attachment_deadline import Transfer
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hermes_cli.kanban_sync_attachment_deadline import Transfer

_deadline: ContextVar[float | None] = ContextVar("ci_http_deadline", default=None)


@contextmanager
def deadline_scope(deadline: float | None):
    outer = _deadline.get()
    effective = outer if deadline is None else deadline if outer is None else min(outer, deadline)
    token = _deadline.set(effective)
    try:
        yield
    finally:
        _deadline.reset(token)


def scoped_poll(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with deadline_scope(kwargs.get("deadline")):
            return fn(*args, **kwargs)
    return wrapped


def remaining(cap: float = 60.0) -> float:
    deadline = _deadline.get()
    budget = cap if deadline is None else min(cap, deadline - time.monotonic())
    if budget <= 0:
        raise TimeoutError("CI read deadline exceeded")
    return budget


def read(request, limit: int, *handlers):
    budget = remaining()
    transfer = Transfer(budget)

    def operation():
        opener = urllib.request.build_opener(*handlers, *transfer.handlers())
        with opener.open(request, timeout=budget) as response:
            body = response.read(limit + 1)
            return response.status, response.headers.get("Location"), body

    return transfer.run(operation)
