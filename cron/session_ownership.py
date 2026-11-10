"""Release conversation ownership without changing a job's execution contract."""
import json
from datetime import UTC, datetime
from pathlib import Path

from cron import jobs
from hermes_cli.cron_session_activity import _job_is_live


def store_homes(db) -> list[Path]:
    """Resolve only the DB's installation, never ambient HERMES_HOME."""
    home = Path(db.db_path).resolve().parent
    root = home.parent.parent if home.parent.name == "profiles" else home
    return list(dict.fromkeys([home, root, *(p.parent.parent for p in sorted(root.glob("profiles/*/cron/jobs.json")))]))


def release_for_database(db, session_ids: list[str], *, keep: bool = False) -> tuple[list[dict], list[str]]:
    released, errors = [], []
    for home in store_homes(db):
        if not (home / "cron" / "jobs.json").exists():
            continue
        try:
            with jobs.use_cron_store(home):
                if keep:
                    with jobs._jobs_lock():
                        records = jobs.load_jobs()
                        changed = False
                        for job in records:
                            if job.get("owner_session_id") in session_ids and not job.get("owner_retained"):
                                job["owner_retained"] = True
                                changed = True
                        if changed:
                            jobs.save_jobs(records)
                else:
                    released.extend(release_session_jobs(session_ids))
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{home}: {exc}")
    return released, errors


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

    plan = []
    for home in store_homes(db):
        if not (home / "cron" / "jobs.json").exists():
            continue
        with jobs.use_cron_store(home), jobs._jobs_lock():
            if apply:
                records = jobs.load_jobs()
            else:
                data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8-sig"))
                records = data.get("jobs", []) if isinstance(data, dict) else data
                if not isinstance(records, list) or any(not isinstance(j, dict) for j in records):
                    raise ValueError("Invalid cron records; migration will not repair them")
            changed = False
            for job in records:
                owner = job.get("owner_session_id")
                if not owner or not _job_is_live(job) or job.get("owner_retained"):
                    continue
                outcome = get_outcome(owner, db=db) or {}
                if outcome.get("kind") != "done" or outcome.get("keep_crons"):
                    continue
                plan.append({"id": job["id"], "name": job.get("name") or job["id"],
                             "owner_session_id": owner, "store": str(home / "cron" / "jobs.json")})
                if apply:
                    job["owner_session_id"] = None
                    job["released_from"] = owner
                    job["released_at"] = datetime.now(UTC).isoformat()
                    changed = True
            if changed:
                jobs.save_jobs(records)
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
    with SessionDB(db_path=args.db) as db:
        plan = release_done_owners(db, apply=args.apply)
    print(json.dumps({"apply": args.apply, "jobs": plan}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
