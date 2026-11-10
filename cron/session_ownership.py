"""Release conversation ownership without changing a job's execution contract."""
from datetime import UTC, datetime

from cron import jobs
from hermes_cli.cron_session_activity import _job_is_live


def release_session_jobs(session_ids: list[str]) -> list[dict]:
    """Revalidate owners under the store lock; preserve every scheduling field."""
    released = []
    with jobs._jobs_lock():
        records = jobs.load_jobs()
        for job in records:
            owner = job.get("owner_session_id")
            if owner in session_ids and _job_is_live(job):
                job["owner_session_id"] = None
                job["released_from"] = owner
                job["released_at"] = datetime.now(UTC).isoformat()
                released.append({"id": job["id"], "name": job.get("name") or job["id"]})
        if released:
            jobs.save_jobs(records)
    return released


def release_done_owners(db, *, apply: bool = False) -> list[dict]:
    """List (or release) live jobs owned by stored-done chats in this store.

    Never derive completion from prose during migration. Explicit keep_crons is
    durable intent, and missing/unknown owners are left alone.
    """
    from hermes_state_outcome import get_outcome

    with jobs._jobs_lock():
        records = jobs.load_jobs()
        owners = set()
        plan = []
        for job in records:
            owner = job.get("owner_session_id")
            if not owner or not _job_is_live(job):
                continue
            outcome = get_outcome(owner, db=db) or {}
            if outcome.get("kind") == "done" and not outcome.get("keep_crons"):
                owners.add(owner)
                plan.append({"id": job["id"], "name": job.get("name") or job["id"]})
        if apply:
            return release_session_jobs(list(owners))
        return plan
