"""A cron run killed mid-turn must not leave its state.db session row unended forever.

The owner process dies before ``_finalize_cron_session`` runs (gateway restart); the next process's
``recover_interrupted_executions`` marks the ledger row ``unknown`` and must also title + end the
session row (end_reason ``cron_interrupted``), without touching a newer live run of the same job.
"""

from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    import hermes_state
    from hermes_state import SessionDB

    # Production code opens the default state.db: point it at this test's file.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = SessionDB(db_path=tmp_path / "state.db")
    yield executions, db, tmp_path
    db.close()


def _session_for(db, execution, job_id, *, offset=0.0, suffix="000001"):
    sid = f"cron_{job_id}_20261029_{suffix}"
    db.create_session(sid, source="cron")
    started = datetime.fromisoformat(execution["claimed_at"]).timestamp() + offset
    db._execute_write(lambda c: c.execute("UPDATE sessions SET started_at=? WHERE id=?", (started, sid)))
    return sid


def _kill_owner(monkeypatch, executions):
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)


def test_recovery_ends_and_titles_interrupted_session(monkeypatch, env):
    executions, db, _ = env
    rec = executions.create_execution("job1", source="builtin")
    executions.mark_execution_running(rec["id"])
    sid = _session_for(db, rec, "job1")
    _kill_owner(monkeypatch, executions)

    assert executions.recover_interrupted_executions() == 1

    row = db.get_session(sid)
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cron_interrupted"
    assert row["title"] and row["title"].endswith("(interrupted)")
    assert row["title"].startswith("cron job1 · ")


def test_recovery_keeps_existing_title_and_uses_job_name(monkeypatch, env):
    executions, db, _ = env
    monkeypatch.setattr("cron.jobs.get_job", lambda jid: {"id": jid, "name": "x reply discovery"})
    rec = executions.create_execution("job2", source="builtin")
    executions.mark_execution_running(rec["id"])
    titled = _session_for(db, rec, "job2")
    db.set_session_title(titled, "already titled")
    _kill_owner(monkeypatch, executions)

    executions.recover_interrupted_executions()

    row = db.get_session(titled)
    assert row["title"] == "already titled"
    assert row["end_reason"] == "cron_interrupted"


def test_recovery_does_not_end_a_newer_live_run_of_the_same_job(monkeypatch, env):
    executions, db, _ = env
    old = executions.create_execution("job3", source="builtin")
    executions.mark_execution_running(old["id"])
    old_sid = _session_for(db, old, "job3", suffix="000001")
    _kill_owner(monkeypatch, executions)
    # The replacement process already claimed and started the next run of the same job.
    new = executions.create_execution("job3", source="builtin")
    executions.mark_execution_running(new["id"])
    new_sid = _session_for(db, new, "job3", suffix="000002")

    assert executions.recover_interrupted_executions() == 1

    assert db.get_session(old_sid)["end_reason"] == "cron_interrupted"
    live = db.get_session(new_sid)
    assert live["ended_at"] is None and live["title"] is None


def test_terminalize_dead_owner_ends_session(monkeypatch, env):
    executions, db, _ = env
    rec = executions.create_execution("job4", source="builtin")
    executions.mark_execution_running(rec["id"])
    sid = _session_for(db, rec, "job4")
    _kill_owner(monkeypatch, executions)

    assert executions.terminalize_dead_owner(rec["id"], reason="worker exited 137") is True
    assert db.get_session(sid)["end_reason"] == "cron_interrupted"


def test_session_cleanup_failure_never_breaks_recovery(monkeypatch, env):
    executions, _db, _ = env
    rec = executions.create_execution("job5", source="builtin")
    executions.mark_execution_running(rec["id"])
    _kill_owner(monkeypatch, executions)
    import cron.interrupted_sessions as mod

    def _boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mod, "_end_sessions_body", _boom)

    assert executions.recover_interrupted_executions() == 1
    assert executions.get_execution(rec["id"])["status"] == "unknown"


def test_backfill_ends_sessions_of_already_recovered_executions(monkeypatch, env):
    executions, db, _ = env
    rec = executions.create_execution("job6", source="builtin")
    executions.mark_execution_running(rec["id"])
    sid = _session_for(db, rec, "job6")
    _kill_owner(monkeypatch, executions)
    import cron.interrupted_sessions as mod

    # Simulate the pre-fix recovery: ledger marked unknown, session left open.
    monkeypatch.setattr(mod, "end_interrupted_cron_sessions", lambda *a, **k: 0)
    executions.recover_interrupted_executions()
    assert db.get_session(sid)["ended_at"] is None
    monkeypatch.undo()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", env[2] / "cron" / "executions.db")
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", env[2] / "state.db")

    assert mod.backfill_interrupted_cron_sessions(days=1) == 1
    assert db.get_session(sid)["end_reason"] == "cron_interrupted"
    assert mod.backfill_interrupted_cron_sessions(days=1) == 0


def test_cleanup_thread_keeps_profile_scoped_hermes_home(monkeypatch, tmp_path):
    """The bounded worker thread must open the state.db of the caller's (context-local) home."""
    from cron import executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import hermes_state
    from hermes_state import SessionDB

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    # Default path resolves from get_hermes_home() (the context-local override), not a pinned file.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    token = set_hermes_home_override(tmp_path)
    try:
        db = SessionDB(db_path=tmp_path / "state.db")
        rec = executions.create_execution("job7", source="builtin")
        executions.mark_execution_running(rec["id"])
        sid = _session_for(db, rec, "job7")
        _kill_owner(monkeypatch, executions)
        assert executions.recover_interrupted_executions() == 1
        assert db.get_session(sid)["end_reason"] == "cron_interrupted"
        db.close()
    finally:
        reset_hermes_home_override(token)
