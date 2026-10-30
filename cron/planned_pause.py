"""Durable eligibility for one-shots deferred by a planned runtime pause.

Each suppressed tick/control request publishes its own interval without taking the
jobs lock (a pause ACK must not wait behind cron I/O). The admitted scan closes and
consumes only its snapshot, so concurrent pause writers cannot be lost at cleanup.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime
from pathlib import Path

from utils import atomic_write_text

logger = logging.getLogger("cron.jobs")


def _path() -> Path:
    from cron.jobs import _current_cron_store
    return _current_cron_store().cron_dir / "planned-pauses"


def _read(path: Path) -> dict | None:
    try:
        window = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(window, dict) or "end" not in window
                or not isinstance(window.get("start"), (int, float)) or not math.isfinite(window["start"])
                or (window["end"] is not None and (
                    not isinstance(window["end"], (int, float)) or not math.isfinite(window["end"])
                    or window["end"] < window["start"]))):
            raise ValueError("invalid pause interval")
        return window
    except FileNotFoundError:
        return None
    except (ValueError, TypeError) as exc:
        logger.warning("Ignoring malformed planned cron pause %s: %s", path, exc)
        return None


def _write(path: Path, window: dict) -> None:
    atomic_write_text(path, json.dumps(window), mode=0o600)


def begin() -> None:
    """Publish before suppressing admission; store failures remain observable."""
    from cron import jobs, store_health
    try:
        directory = _path()
        jobs._ensure_cron_dir(directory)
        _write(directory / f"{uuid.uuid4().hex}.json", {"start": jobs._hermes_now().timestamp(), "end": None})
    except OSError as exc:
        store_health.note_unwritable(exc, "planned cron pause could not be recorded", "pause")


def recover(raw_jobs: list[dict], now: datetime) -> tuple[bool, list[Path]] | None:
    """Under the jobs lock, pin interval ends before modifying jobs; None means retry writes.

    A crash before the jobs save replays the same bounded intervals, never extends them
    to later slots. The caller removes this snapshot only after its jobs save lands.
    """
    from cron import jobs, jobs_time, store_health
    try:
        paths = list(_path().glob("*.json"))
        windows = []
        for path in paths:
            window = _read(path)
            if window is None:
                continue
            if window["end"] is None:
                window["end"] = max(now.timestamp(), window["start"])
                _write(path, window)
            windows.append(window)
    except OSError as exc:
        store_health.note_unwritable(exc, "planned cron pause recovery deferred", "pause")
        return None
    changed = False
    for job in raw_jobs:
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "once" or not job.get("enabled", True):
            continue
        due = jobs._parse_aware(job.get("next_run_at"), jobs_time.zone_for_job(job))
        if due is not None and any(
            w["start"] - jobs.ONESHOT_GRACE_SECONDS <= due.timestamp() <= w["end"] for w in windows
        ):
            job["planned_pause_slot"] = job["next_run_at"]
            changed = True
    return changed, paths


def covers(job: dict) -> bool:
    """Editing or resuming onto a different instant invalidates the recovery exemption."""
    return bool(job.get("next_run_at") and job.get("planned_pause_slot") == job["next_run_at"])


def retire_expired(d) -> bool:
    """Retire stale one-shots, except occurrences deliberately held through a pause/outage."""
    from cron import jobs, store_health
    grace = jobs.ONESHOT_GRACE_SECONDS
    if covers(d.job) or jobs._elapsed_seconds(d.scan.now, d.next_run_dt) <= grace:
        return False
    if store_health.outage_covers(jobs._current_cron_store().cron_dir, d.next_run_dt.timestamp(), grace):
        return False
    claim = d.job.get("run_claim")
    if isinstance(claim, dict) and claim.get("outage") and not d.job.get("fire_claim"):
        return False
    if not (claim or d.job.get("fire_claim")):
        from cron.jobs_oneshot_diagnostics import write_missed
        write_missed(d.job, d.next_run)
        d.scan.retire(d.job["id"])
        d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
    return True


def finish_recovery(paths: list[Path]) -> None:
    """The jobs save landed; do not remove intervals concurrently published by another ticker."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove recovered cron pause %s: %s", path, exc)
