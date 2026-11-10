"""Declaring done ends ownership, not execution, of standing cron jobs."""
import sqlite3

import pytest

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


def test_failed_declaration_write_does_not_release_jobs(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        before = jobs.get_job(job["id"])

        def fail_write(*args):
            raise sqlite3.OperationalError("injected write failure")

        monkeypatch.setattr(outcomes, "_write", fail_write)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        assert jobs.get_job(job["id"]) == before


def test_corrupt_cron_store_reports_partial_completion(tmp_path):
    cron_file = tmp_path / "cron" / "jobs.json"
    cron_file.parent.mkdir()
    cron_file.write_text("broken json", encoding="utf-8")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        result = outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        assert result["kind"] == "done"
        assert result["cron_release_error"]
        assert outcomes.get_outcome("chat", db=db) == result
    assert cron_file.read_text(encoding="utf-8") == "broken json"


def test_done_releases_cross_profile_owner_not_unrelated_jobs(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "other"
    profile.mkdir(parents=True)
    with jobs.use_cron_store(profile):
        owned = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        unrelated = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="someone-else")
    monkeypatch.setattr(cron_session_activity, "_jobs_paths", lambda: [profile / "cron" / "jobs.json"])
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        result = outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        assert [j["id"] for j in result["released_crons"]] == [owned["id"]]
        assert outcomes._cron_items(["chat"]) == []
    with jobs.use_cron_store(profile):
        assert jobs.get_job(unrelated["id"]) == unrelated


def test_done_can_keep_cron_ownership(tmp_path):
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        before = jobs.get_job(job["id"])
        with SessionDB(db_path=tmp_path / "state.db") as db:
            db.create_session("chat", source="desktop")
            result = outcomes.declare_outcome("chat", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)
            assert before is not None
            assert jobs.get_job(job["id"]) == {**before, "owner_retained": True}
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


def test_migration_preserves_keep_intent_after_outcome_replaced(tmp_path):
    from cron import session_ownership

    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)
        outcomes._write(db, "chat", {"kind": "done", "source": "derived", "summary": "Later followup complete."})
        assert session_ownership.release_done_owners(db, apply=True) == []
        assert jobs.get_job(job["id"])["owner_session_id"] == "chat"
        # A later explicit done without keep is an intentional release.
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Stop owning the watch."}, db=db)
        assert not jobs.get_job(job["id"])["owner_session_id"]


def test_migration_binds_database_and_releases_only_planned_jobs(tmp_path):
    from cron import session_ownership

    home = tmp_path / "installation"
    home.mkdir()
    with jobs.use_cron_store(home), SessionDB(db_path=home / "state.db") as db:
        db.create_session("chat", source="desktop")
        kept = jobs.create_job(prompt="Keep", schedule="every 1h", owner_session_id="chat")
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)
        fresh = jobs.create_job(prompt="Release", schedule="every 1h", owner_session_id="chat")
        outcomes._write(db, "chat", {"kind": "done", "source": "derived", "summary": "Finished."})
        with jobs.use_cron_store(tmp_path):
            foreign = jobs.create_job(prompt="Foreign", schedule="every 1h", owner_session_id="chat")
            plan = session_ownership.release_done_owners(db)
            assert [r["id"] for r in plan] == [fresh["id"]]
            assert plan[0]["owner_session_id"] == "chat"
            assert session_ownership.release_done_owners(db, apply=True) == plan
            assert jobs.get_job(foreign["id"]) == foreign
        assert jobs.get_job(kept["id"])["owner_session_id"] == "chat"
        assert not jobs.get_job(fresh["id"])["owner_session_id"]


def test_migration_dry_run_does_not_repair_legacy_store(tmp_path):
    import json
    from cron import session_ownership

    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        path = tmp_path / "cron" / "jobs.json"
        raw = json.dumps([job])
        path.write_text(raw, encoding="utf-8")
        assert session_ownership.release_done_owners(db)[0]["id"] == job["id"]
        assert path.read_text(encoding="utf-8") == raw


def test_migration_cli_lists_owners_before_apply(tmp_path, capsys):
    import json
    from cron import session_ownership

    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        args = ["--db", str(tmp_path / "state.db")]
        assert session_ownership.main(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["apply"] is False
        assert report["jobs"][0]["owner_session_id"] == "chat"
        assert jobs.get_job(job["id"])["owner_session_id"] == "chat"
        assert session_ownership.main([*args, "--apply"]) == 0
        assert json.loads(capsys.readouterr().out)["apply"] is True
        assert not jobs.get_job(job["id"])["owner_session_id"]


def test_refresh_removes_migrated_cron_from_stored_declaration(tmp_path, monkeypatch):
    from cron import session_ownership

    monkeypatch.setattr(cron_session_activity, "_jobs_paths", lambda: [tmp_path / "cron" / "jobs.json"])
    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Finished."}, db=db)
        job = jobs.create_job(prompt="Check", schedule="every 1h", owner_session_id="chat")
        assert outcomes.refresh_outcome("chat", "turn_end", db=db)["crons"][0]["id"] == job["id"]
        session_ownership.release_done_owners(db, apply=True)
        refreshed = outcomes.refresh_outcome("chat", "turn_end", db=db)
        assert refreshed["kind"] == "done"
        assert not refreshed.get("crons")


def test_keep_does_not_write_unrelated_store(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path), SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", source="desktop")
        jobs.create_job(prompt="Other", schedule="every 1h", owner_session_id="other")
        def reject_save(*args, **kwargs):
            pytest.fail("unrelated cron store was rewritten")
        monkeypatch.setattr(jobs, "save_jobs", reject_save)
        outcomes.declare_outcome("chat", {"kind": "done", "summary": "Watching.", "keep_crons": True}, db=db)


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
