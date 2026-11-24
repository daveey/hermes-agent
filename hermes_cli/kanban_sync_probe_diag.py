"""Passive loop-only probe census. Answers are observed, never reused.

The first 32 valid slugs encountered in loop-list order retain their names for
this process's lifetime. Other paths fold into ``unmapped`` (including a real
board with that name). Wait history is
bounded by that snapshot's paths and is cleared when the loop leaves its pool
thread. Counters overlap all enclosing CPU scopes and carry zero CPU themselves.
"""
import threading
import time

from hermes_cli import kanban_sync_cpu as cpu, loop_accounting

_local = threading.local()
_names = set()  # process-lifetime series bound, including across loop tenures
_names_lock = threading.Lock()


def active():
    return getattr(_local, "active", False)


def enter():
    _local.active = True
    _local.paths, _local.waits = {}, {}


def leave():
    _local.active = False
    _local.paths, _local.waits = {}, {}


def install(boards):
    """Only the loop caller installs its own board_paths result, once per round."""
    if not active():
        return
    from hermes_cli.kanban_sync_client import BOARD_ID_RE
    paths = {}
    with _names_lock:
        for slug, path in boards:
            valid = isinstance(slug, str) and BOARD_ID_RE.fullmatch(slug)
            if valid and (slug in _names or len(_names) < 32):
                _names.add(slug)
                paths[str(path)] = slug
            else:
                paths[str(path)] = "unmapped"
    _local.paths = paths
    _local.waits = {p: v for p, v in _local.waits.items() if p in paths}


def _record(kind, slug, rows=0):
    loop_accounting.record(f"sync.probe_{kind}/slug/{slug}", cpu_s=0, rows=rows, outer=False)


def note(path, got):
    """Observe an already-executed query; even unavailable/empty answers are negative.

    Unknown paths count under unmapped but are not retained as wait history.
    Integer-ms rows use the existing accounting truncation (sub-ms contributes 0).
    """
    if not active():
        return
    try:
        key = str(path)
        slug = _local.paths.get(key, "unmapped")
        _record("count", slug)
        step = cpu._step_name()
        now = time.monotonic()
        if step == "wait" and key in _local.paths:
            _local.waits[key] = (now, bool(got is not None and got[1]))
        elif step == "select":
            prior = _local.waits.get(key)
            age = max(0.0, now - prior[0]) if prior else None
            hit = prior is not None and not prior[1] and age <= 0.5
            _record("reuse_hit" if hit else "reuse_miss", slug)
            if age is not None:
                _record("wait_age_ms", slug, int(age * 1000))
    except Exception:  # health: allow BLE001 -- passive diagnostics must not alter query behavior
        return
