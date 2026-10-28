"""Cross-gateway card waits survive an unavailable SSH route without hiding broken reads."""
import json
import sqlite3
import subprocess
import time

import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_wake_check as wake

TID = "t_123456789abcdef0"


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    db = sqlite3.connect(tmp_path / "hub.db")
    db.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    scope = {"task_scopes": [{"board": "default", "profile": "default", "state": "ok", "observed_at": time.time()}]}
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "gateway", "other", json.dumps(scope), int(time.time()*1000)))
    db.commit()
    monkeypatch.setattr(wait.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 255, "", "Could not resolve hostname"))
    yield db
    db.close()


def put(db, status, **fields):
    row = dict(id=TID, board="default", status=status, gateway="other", profile="default", owner_profile="default",
               wait_observation={"parked_agent": False, "orphan_reason": None}, **fields)
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "tasks", f"default/default/{TID}", json.dumps(row), 1))
    db.commit()


@pytest.mark.parametrize("status,rc", [("done", 0), ("archived", 0), ("blocked", 0), ("running", 1), ("scheduled", 1)])
def test_fallback_status_including_terminal_records(replica, status, rc):
    put(replica, status)
    code, text = wait.check_remote("other", "task", TID)
    assert code == rc, text
    assert text.split()[:2] == [TID, status]


def test_missing_and_stale_are_errors(replica):
    assert wait.check_remote("other", "task", TID)[0] == 2
    put(replica, "done")
    replica.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps({"task_scopes": [{"profile": "default", "board": "default", "state": "ok", "observed_at": 1}]}),))
    replica.commit()
    code, text = wait.check_remote("other", "task", TID)
    assert code == 2 and "stale" in text


def test_since_and_until_preserved(replica):
    put(replica, "done")
    assert wait.check_remote("other", "task", TID, since=("done",))[0] == 1
    assert wait.check_remote("other", "task", TID, until=("blocked",))[0] == 1


def test_confirmation_accepts_live_but_rejects_orphan(replica):
    put(replica, "running")
    awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))
    replica.execute("DELETE FROM replica WHERE coll='tasks'")
    put(replica, "blocked", human_request=None, block_kind="transient")
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_handrolled_replica_probe_refused(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    cmd = f"hermes-hub tasks --gw other --json | python3 -c 'print(\"{TID}\")'"
    error = wake.dry_run_error(cmd)
    assert error and "hermes kanban wait-check task" in error


def test_qualified_wins_over_legacy_and_legacy_alone_is_unknown(replica):
    put(replica, "done")
    replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "tasks", f"default/{TID}", json.dumps(dict(id=TID, board="default", status="running")), 1))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 0
    replica.execute("DELETE FROM replica WHERE key=?", (f"default/default/{TID}",))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2


@pytest.mark.parametrize("failure", [OSError("no ssh"), subprocess.TimeoutExpired("ssh", 25)])
def test_transport_exceptions_fall_back(replica, monkeypatch, failure):
    put(replica, "done")
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(wait.subprocess, "run", fail)
    assert wait.check_remote("other", "task", TID)[0] == 0


@pytest.mark.parametrize("status", ["archived", "triage"])
def test_non_live_confirmation(replica, status):
    put(replica, status)
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_scheduled_evidence_missing_is_error_and_orphan_wakes(replica):
    put(replica, "scheduled")
    row = json.loads(replica.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
    row.pop("wait_observation")
    replica.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(row),))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2
    row["wait_observation"] = dict(parked_agent=True, orphan_reason="parked with no producer")
    replica.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(row),))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 0
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_automatic_probe_since_status(replica, monkeypatch, capsys):
    from argparse import Namespace
    put(replica, "done")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_PROBE", "1")
    monkeypatch.setattr(wait, "handled_status", lambda *a: "done")
    assert wait.run(Namespace(check="task", target=TID, gateway="other", board=None, until=None, since=None)) == 1
    assert "unchanged since park" in capsys.readouterr().out


def test_scope_and_identity_fail_closed(replica):
    put(replica, "done")
    assert wait.check_remote("other", "task", TID, board="elsewhere")[0] == 2
    assert wait.check_remote("elsewhere", "task", TID)[0] == 2
    replica.execute("DELETE FROM replica WHERE coll='gateway'")
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2


def test_supported_probe_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    assert wake.dry_run_error(f"hermes kanban wait-check task {TID} --gateway other") is None


def test_owner_collector_publishes_wait_evidence(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect
    conn = connect()
    try:
        tid = kb.create_task(conn, title="collector fixture", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "scheduled", {"reason": "denied access; needs permission"})
        conn.execute("BEGIN")
        row = observe_owner_task(conn, dict(id=tid, status="scheduled", gateway="other", profile="default", board="default"), now=time.time())
        assert row["wait_observation"]["orphan_reason"]
        assert row["wait_observation"]["parked_agent"] is True
        conn.rollback()
        rc, out = wait.check_local("task", tid)
        assert "orphan producer" in out or "agent-owned park:" in out
        monkeypatch.setattr(wait.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, rc, out, ""))
        with pytest.raises(awaiting.AwaitingError):
            awaiting.confirm_remote(dict(kind="card", ref=tid, gateway="other"))
    finally:
        conn.close()
