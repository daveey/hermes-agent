"""Read-only persisted child activity, on the existing session annotation cadence.

One indexed SELECT per requested profile/parent set per three seconds; no liveness claims from absence.
Unended children older than the six-hour discovery window and persist-disabled children are
not covered; stale rows inside the window remain explicit unknown observations.
"""
from __future__ import annotations
from hermes_cli.kanban_annotation_facts import consumer as _fact_consumer

import json
import logging
from collections import OrderedDict
import sqlite3
import threading
import time
from pathlib import Path

from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS, local_scope
from hermes_cli.turn_process_execution_observations import SUBAGENT_SOURCES, replace, subagent

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache = OrderedDict()
_WINDOW = 6 * 3600


def _profile_paths():
    from hermes_constants import get_hermes_home
    from hermes_cli.turn_process_execution_observations import profile_for_state_path

    home = Path(get_hermes_home())
    root = home.parent.parent if home.parent.name == "profiles" else home
    paths = [root / "state.db", *sorted((root / "profiles").glob("*/state.db"))]
    return [(profile_for_state_path(path), path) for path in paths]


def _children(path, now, parents):
    mono = time.monotonic()
    key = (path, tuple(sorted(parents)))
    with _lock:
        cached = _cache.get(key)
        if cached is not None and mono < cached[0]:
            _cache.move_to_end(key)
            return cached[1]
        # URI mode=ro never creates a store or invokes migrations. A read failure is
        # unknown, not a successful empty inventory; do not fall back to mode=rw.
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                rows = conn.execute(
                    "SELECT id, parent_session_id, last_activity_at, started_at FROM sessions INDEXED BY idx_sessions_parent "
                    "WHERE source='subagent' AND parent_session_id IN (SELECT value FROM json_each(?)) "
                    "AND ended_at IS NULL AND started_at >= ?", (json.dumps(key[1]), now - _WINDOW)).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            rows = None
        _cache[key] = (mono + 3, rows)
        _cache.move_to_end(key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
        return rows


@_fact_consumer('subagent')
def annotate_subagent_activity(rows):
    """Append canonical observations, never count a missing/failed store as zero work."""
    if not rows:
        return rows
    now = time.time()
    try:
        gateway, _ = local_scope()
        paths = _profile_paths()
    except ADAPTER_ERRORS:
        _log.debug("subagent scope unavailable", exc_info=True)
        gateway, paths = None, []
    parents_by_profile = {}
    for row in rows:
        parents = parents_by_profile.setdefault(row.get('profile') or 'default', set())
        parents.update(parent for parent in (row.get('id'), row.get('_lineage_root_id'),
                                             *(row.get('_lineage_ids') or ())) if isinstance(parent, str))
    by_profile = {}
    for profile, path in paths:
        if profile not in parents_by_profile:
            continue
        children = _children(path, now, parents_by_profile[profile])
        by_profile[profile] = (children or [], "subagent_store_unavailable" if children is None else None)
    return project_subagent_activity(rows, by_profile=by_profile, gateway=gateway, now=now)


def project_subagent_activity(rows, *, by_profile, gateway, now):
    """Pure serializer; profile maps to (child tuples, optional source error)."""
    for row in rows:
        profile = row.get("profile") or "default"
        children, error = by_profile.get(profile, ([], "subagent_store_unavailable"))
        parents = {row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())}
        observations = [subagent({"child_session_id": child, "parent_session_id": parent,
                                 "activity_at": activity, "started_at": started, "live": True},
                                gateway=gateway, profile=profile, now=now)
                        for child, parent, activity, started in children if parent in parents]
        if error:
            # A source-level unknown has no fabricated execution identity. Preserve
            # known children so a failed read cannot erase their last source clocks.
            previous = row.get("execution_observations") or []
            observations.extend({**o, "state": "unknown", "unknown_reason": error}
                                for o in previous if isinstance(o, dict) and o.get("source") in SUBAGENT_SOURCES)
            unknown = subagent({"parent_session_id": row.get("id"), "live": True},
                               gateway=gateway, profile=profile, now=now)
            unknown["unknown_reason"] = error
            observations.append(unknown)
        replace(row, SUBAGENT_SOURCES, observations)
    return rows


def _reset_cache_for_tests():
    with _lock:
        _cache.clear()
