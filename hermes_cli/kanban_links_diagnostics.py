"""Diagnostic-only links attribution. No IO, extra freshness probes or cache policy.

Reason counters are zero-CPU events, not additive timing spans. Board work and
source fingerprint/projection spans are inclusive subsets of their callers.
Enter minus exit is in-flight work; a bracket is quiet only with equal counters
at BOTH endpoints and no enters between them. Names retain at most 16 board
slugs (then ``other``), independent of the process-wide accounting name bound.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading


from hermes_cli import loop_accounting as la

_lane = ContextVar("links_diagnostic_lane", default="links_other")
_boards: dict[str, str] = {}
_lock = threading.Lock()
_COMPONENTS = ("identity", "revision", "replica", "ledger", "mode", "signature")


def event(name: str) -> None:
    la.record(name, outer=False)


def board_name(slug: str) -> str:
    if len(slug) > 80:
        return "sync.links.board.other"
    with _lock:
        name = _boards.get(slug)
        if name is None:
            if len(_boards) >= 16:
                return "sync.links.board.other"
            # Slugs are existing board identifiers, not paths or card/session IDs.
            name = "sync.links.board." + slug
            _boards[slug] = name
        return name


def board_reason(slug: str, reason: str, old=None, new=None) -> None:
    prefix = board_name(slug)
    event(prefix + ".reason." + reason)
    if old is not None and new is not None:
        for component, before, after in zip(_COMPONENTS, old, new):
            if before != after:
                event(prefix + ".changed." + component)


@contextmanager
def source(lane: str):
    token = _lane.set(lane)
    event(lane + ".enter")
    try:
        yield
    finally:
        event(lane + ".exit")
        _lane.reset(token)


def timed(part: str, fn, *args):
    return la.timed(_lane.get() + "." + part, fn, *args)


def source_decision(hit, fingerprint, fresh: bool, now: float) -> None:
    lane = _lane.get()
    if fresh:
        event(lane + ".hit")
        return
    reason = "missing" if hit is None else (
        "age_inputs" if now >= hit[0] and hit[2] != fingerprint else
        "age" if now >= hit[0] else "inputs")
    event(lane + ".miss." + reason)


def published() -> None:
    event(_lane.get() + ".publish")


def board_decision(done, inputs, started, max_age, reads) -> str:
    """Original short-circuit admission, with its reason exposed; never replay twice."""
    if inputs is None:
        return "degraded"
    if done is None:
        return "missing"
    if done[0] != inputs:
        return "changed"
    if done[2] is None:
        return "age" if started - done[1] >= max_age else "skip"
    if not reads.unchanged(done[2]):
        return "degraded" if reads.degraded else "chat-replay"
    return "skip"


def refresh(scope, cache, fingerprint_fn, fresh_fn, publish_fn, clock) -> None:
    """Instrument the existing background decision under its caller-owned lock."""
    from hermes_cli.kanban_links_refresh import refresh_decided
    with source("links_refresh"):
        fingerprint = timed("fingerprint", fingerprint_fn)
        hit = cache.get(scope)
        fresh = hit is not None and fresh_fn(hit, fingerprint)
        refresh_decided(hit, fingerprint, fresh, clock(), lambda: publish_fn(scope, fingerprint))
