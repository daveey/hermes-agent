"""Release conversation ownership without changing a job's execution contract."""
import json
from datetime import UTC, datetime
from pathlib import Path

from cron import jobs
from hermes_cli.cron_session_activity import _job_is_live


def _read_records() -> list[dict]:
    """Read under the caller's store lock without repairing unrelated metadata."""
    path = jobs._current_cron_store().jobs_file
    stamp = jobs._jobs_file_stamp(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    records = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(records, list) or any(
        not isinstance(j, dict) or not isinstance(j.get("id"), str) or not j["id"]
        for j in records
    ):
        raise ValueError("Invalid cron records; ownership changes will not repair them")
    jobs._record_load_stamp(stamp)
    return records


def store_homes(db) -> list[Path]:
    """Resolve only the DB's installation, never ambient HERMES_HOME."""
    home = Path(db.db_path).resolve().parent
    root = home.parent.parent if home.parent.name == "profiles" else home
    return list(dict.fromkeys([home, root, *(p.parent.parent for p in sorted(root.glob("profiles/*/cron/jobs.json")))]))


def _retain_session_jobs(session_ids: list[str]) -> None:
    with jobs._jobs_lock():
        records = _read_records()
        changed = False
        for job in records:
            if job.get("owner_session_id") in session_ids and job.get("owner_retained") != job.get("owner_session_id"):
                job["owner_retained"] = job["owner_session_id"]
                changed = True
        if changed:
            jobs.save_jobs(records)


def release_for_database(db, session_ids: list[str], *, keep: bool = False) -> tuple[list[dict], list[str]]:
    released, errors = [], []
    for home in store_homes(db):
        if not (home / "cron" / "jobs.json").exists():
            continue
        try:
            with jobs.use_cron_store(home):
                if keep:
                    _retain_session_jobs(session_ids)
                else:
                    released.extend(release_session_jobs(session_ids))
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{home}: {exc}")
    return released, errors


def release_session_jobs(session_ids: list[str]) -> list[dict]:
    """Revalidate owners under the store lock; preserve every scheduling field."""
    released = []
    with jobs._jobs_lock():
        records = _read_records()
        for job in records:
            owner = job.get("owner_session_id")
            if owner in session_ids and _job_is_live(job):
                job["owner_session_id"] = None
                job.pop("owner_retained", None)
                job["released_from"] = owner
                job["released_at"] = datetime.now(UTC).isoformat()
                released.append({"id": job["id"], "name": job.get("name") or job["id"]})
        if released:
            jobs.save_jobs(records)
    return released


def _migrate_store(db, home: Path, apply: bool) -> list[dict]:
    from hermes_state_outcome import get_outcome

    plan = []
    with jobs.use_cron_store(home), jobs._jobs_lock():
        records = _read_records()
        for job in records:
            owner = job.get("owner_session_id")
            if not owner or not _job_is_live(job) or job.get("owner_retained") == owner:
                continue
            outcome = get_outcome(owner, db=db) or {}
            if outcome.get("kind") != "done" or outcome.get("keep_crons"):
                continue
            plan.append({"id": job["id"], "name": job.get("name") or job["id"],
                         "owner_session_id": owner, "store": str(home / "cron" / "jobs.json")})
            if apply:
                job["owner_session_id"] = None
                job.pop("owner_retained", None)
                job["released_from"] = owner
                job["released_at"] = datetime.now(UTC).isoformat()
        if apply and plan:
            jobs.save_jobs(records)
    return plan


def release_done_owners(db, *, apply: bool = False, errors: list[str] | None = None) -> list[dict]:
    """Use stored done (declared or derived), never run a new prose judge.

    Explicit keep intent and missing/unknown owners are left alone. With an
    errors collector, return successful stores even if a later store fails.
    """
    plan = []
    for home in store_homes(db):
        if not (home / "cron" / "jobs.json").exists():
            continue
        try:
            plan.extend(_migrate_store(db, home, apply))
        except (OSError, RuntimeError, ValueError) as exc:
            if errors is None:
                raise
            errors.append(f"{home}: {exc}")
    return plan


def main(argv=None) -> int:
    """``python -m cron.session_ownership --db HOME/state.db [--apply]``."""
    import argparse
    from hermes_state import SessionDB

    parser = argparse.ArgumentParser(description="Release stored-done cron owners; dry-run by default.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("--db must name an existing session database")
    errors: list[str] = []
    with SessionDB(db_path=args.db) as db:
        plan = release_done_owners(db, apply=args.apply, errors=errors)
    print(json.dumps({"apply": args.apply, "jobs": plan, "errors": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
