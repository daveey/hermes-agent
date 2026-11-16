"""Transient remote-index retries; no change to the index's first-miss bound. [daveey]"""
from contextlib import contextmanager

KEY = "_kanban_remote_retry_at"


def cancel(session):
    session.pop(KEY, None)


def deadline(session, now):
    if (session.get("_closing") or session.get("_finalized") or session.get("_lease_taken_over")
            or float(session.get("_kanban_refused_until") or 0) > now):
        cancel(session)
    return session.get(KEY)


@contextmanager
def attempt(session, clock):
    """Only a successful, still-eligible attempt may leave a newly scheduled retry."""
    cancel(session)
    completed = False
    try:
        yield
        completed = True
    finally:
        if not completed:
            cancel(session)
        deadline(session, clock())
