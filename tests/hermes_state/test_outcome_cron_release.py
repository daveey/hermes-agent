"""Declaring done ends ownership, not execution, of standing cron jobs."""
from cron import jobs
from hermes_cli import cron_session_activity
from hermes_state import SessionDB
import hermes_state_outcome as outcomes


def test_done_releases_live_cron_without_changing_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_session_activity, "_jobs_paths", lambda: [tmp_path / "cron" / "jobs.json"])
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="Check status", schedule="every 1h", name="status-watch", owner_session_id="chat")
        before = jobs.get_job(job["id"])
        with SessionDB(db_path=tmp_path / "state.db") as db:
            db.create_session("chat", source="desktop")
            result = outcomes.declare_outcome("chat", {"kind": "done", "summary": "Installed watch."}, db=db)
            after = jobs.get_job(job["id"])
            assert before is not None and after is not None
            assert not after.get("owner_session_id")
            assert after["released_from"] == "chat"
            assert after["released_at"]
            assert result["released_crons"][0]["id"] == job["id"]
            assert outcomes._cron_items(["chat"]) == []
            for key in before:
                if key != "owner_session_id":
                    assert after[key] == before[key], key


def test_done_can_keep_cron_ownership(tmp_path):
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        before = jobs.get_job(job["id"])
        with SessionDB(db_path=tmp_path / "state.db") as db:
            db.create_session("chat", source="desktop")
            result = outcomes.declare_outcome("chat", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)
            assert jobs.get_job(job["id"]) == before
            assert result["keep_crons"] is True


def test_audit_flags_stored_done_with_live_cron(tmp_path, monkeypatch):
    from hermes_cli.sessions_cmd_outcome import audit_rows

    monkeypatch.setattr(cron_session_activity, "_jobs_paths", lambda: [tmp_path / "cron" / "jobs.json"])
    with jobs.use_cron_store(tmp_path):
        with SessionDB(db_path=tmp_path / "state.db") as db:
            db.create_session("chat", source="desktop")
            outcomes.declare_outcome("chat", {"kind": "done", "summary": "Installed."}, db=db)
            job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
            row = next(row for row in audit_rows(db) if row["id"] == "chat")
            assert row["reason"] == "done_owns_cron"
            assert row["on"][0]["id"] == job["id"]


def test_migration_is_dry_by_default_and_preserves_non_done_owners(tmp_path):
    from cron import session_ownership

    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        for sid in ("done", "waiting", "watch"):
            db.create_session(sid, source="desktop")
        outcomes.declare_outcome("done", {"kind": "done", "summary": "Finished."}, db=db)
        outcomes.declare_outcome("watch", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)
        records = [jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id=sid)
                   for sid in ("done", "waiting", "watch", "missing")]
        before = jobs.load_jobs()
        plan = session_ownership.release_done_owners(db)
        assert [r["id"] for r in plan] == [records[0]["id"]]
        assert jobs.load_jobs() == before
        assert session_ownership.release_done_owners(db, apply=True) == plan
        after = jobs.load_jobs()
        assert not after[0].get("owner_session_id")
        assert after[1:] == before[1:]
        assert session_ownership.release_done_owners(db, apply=True) == []
