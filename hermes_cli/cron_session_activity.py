"""Which sessions own a live recurring cron job.

A session that sets up a cron job ("check X every 10 minutes") finishes its turn
and the sidebar painted it "done" (green stripe), although the work goes on
periodically without it. Jobs created from a session record it as
``owner_session_id`` (``cron.jobs.create_job``); session-list endpoints stamp
each row whose lineage owns an enabled, scheduled job (``cron_active``). The
desktop shows a loop glyph there and treats the row like a parked (grey) one.
Paused, disabled and finished jobs don't count. [daveey]

Read-only and cheap: one ``jobs.json`` read per profile, re-read only when the
file changes, plus a short TTL because the sidebar polls.
"""

from __future__ import annotations
from hermes_cli.kanban_annotation_facts import consumer as _fact_consumer

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None
_file_cache: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


def _jobs_paths() -> list[Path]:
    from hermes_constants import get_default_hermes_root, get_hermes_home

    paths: list[Path] = []
    seen: set[str] = set()
    roots = [get_hermes_home()]
    try:
        roots.append(get_default_hermes_root())
    except Exception:  # pragma: no cover - defensive
        pass
    candidates: list[Path] = []
    for root in roots:
        candidates.append(Path(root) / "cron" / "jobs.json")
        prof = Path(root) / "profiles"
        try:
            if prof.is_dir():
                candidates.extend(sorted(prof.glob("*/cron/jobs.json")))
        except OSError:
            pass
    for p in candidates:
        try:
            key = str(p.resolve()) if p.exists() else ""
        except OSError:
            key = ""
        if key and key not in seen:
            seen.add(key)
            paths.append(p)
    return paths


def _job_is_live(job: dict[str, Any]) -> bool:
    if not job.get("enabled", True):
        return False
    if str(job.get("state") or "scheduled") in ("paused", "completed", "disabled"):
        return False
    return bool(job.get("next_run_at"))


def owners_from_jobs(jobs: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for job in jobs:
        if isinstance(job, dict) and _job_is_live(job):
            owner = str(job.get("owner_session_id") or "").strip()
            if owner:
                out.add(owner)
    return out


def _read_owners(path: Path) -> frozenset[str]:
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _file_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    data = json.loads(path.read_text(encoding="utf-8-sig") or "[]")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    owners = frozenset(owners_from_jobs(jobs if isinstance(jobs, list) else []))
    _file_cache[str(path)] = (stamp, owners)
    return owners


def cron_owner_sessions() -> frozenset[str]:
    """Session ids (across every profile) that own a live scheduled cron job."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1]
    ids: set[str] = set()
    for path in _jobs_paths():
        try:
            ids |= _read_owners(path)
        except (OSError, ValueError) as exc:
            _log.debug("cron activity read failed for %s: %s", path, exc)
    result = frozenset(ids)
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, result)
    return result


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


@_fact_consumer('cron')
def annotate_cron_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``cron_active: True`` on rows whose lineage owns a live cron job.
    Never raises: the list must render without it."""
    if not rows:
        return rows
    try:
        owners = cron_owner_sessions()
    except Exception:
        _log.debug("cron activity unavailable", exc_info=True)
        return rows
    return project_cron_activity(rows, owners)


def project_cron_activity(rows, owners):
    """Pure canonical serializer; owners is the captured set of cron-owning session IDs."""
    for row in rows:
        if owners and any(sid in owners for sid in _row_ids(row)):
            row["cron_active"] = True
        else:
            row.pop("cron_active", None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
        _file_cache.clear()
