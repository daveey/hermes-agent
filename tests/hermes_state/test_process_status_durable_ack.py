"""Process acknowledgements must survive the process_manage caller's registry. [daveey]"""
from __future__ import annotations

import json

import pytest

from tests.hermes_state import test_session_status_refresh as helpers
from tools import process_registry as registry_module
from tools import process_registry_results as results


board = helpers.board
receipts = helpers.receipts
db = helpers.db


def receipt(directory, **kwargs):
    import time

    # These are current-generation exits. Filesystem mtimes can lag the SQLite
    # user timestamp on CI; an earlier exit is correctly cleared by a successful
    # successor turn, which would make these acknowledgement assertions vacuous.
    return helpers.receipt(directory, at=time.time() + 1, **kwargs)


@pytest.mark.parametrize("method", ["poll", "wait", "read_log"])
def test_acknowledged_retained_failure_survives_a_fresh_registry(db, receipts, monkeypatch, method):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_retained")
    assert [f["id"] for f in helpers.refresh(db)["failures"]] == ["process:proc_retained"]
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    caller = registry_module.ProcessRegistry()
    if method == "poll":
        result = caller.poll("proc_retained", acknowledge=True)
    else:
        result = getattr(caller, method)("proc_retained")
    assert result["status"] == "exited"
    fresh = registry_module.ProcessRegistry()
    monkeypatch.setattr(registry_module, "process_registry", fresh)
    assert fresh._completion_consumed == set()
    assert helpers.refresh(db)["failures"] == []


def test_plain_poll_does_not_clear_a_retained_failure(db, receipts, monkeypatch):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_unhandled")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    assert registry_module.ProcessRegistry().poll("proc_unhandled")["status"] == "exited"
    assert [f["id"] for f in helpers.refresh(db)["failures"]] == ["process:proc_unhandled"]


def test_ack_does_not_apply_to_another_process_incarnation(db, receipts, monkeypatch):
    helpers.turn(db)
    path = receipt(receipts, exit_code=1, pid="proc_reused")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    caller = registry_module.ProcessRegistry()
    assert caller.poll("proc_reused", acknowledge=True)["status"] == "exited"
    record = json.loads(path.read_text())
    record["started_at"] += 1
    path.write_text(json.dumps(record))
    import os
    os.utime(path, (record["started_at"] + 1, record["started_at"] + 1))
    monkeypatch.setattr(registry_module, "process_registry", registry_module.ProcessRegistry())
    assert [f["id"] for f in helpers.refresh(db)["failures"]] == ["process:proc_reused"]


def test_late_result_rewrite_cannot_erase_acknowledgement(db, receipts, monkeypatch):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_rewrite")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    caller = registry_module.ProcessRegistry()
    session = caller.get("proc_rewrite")
    assert caller.poll("proc_rewrite", acknowledge=True)["status"] == "exited"
    results.save_completed_result(session)
    monkeypatch.setattr(registry_module, "process_registry", registry_module.ProcessRegistry())
    assert helpers.refresh(db)["failures"] == []


def test_ack_for_another_owner_does_not_clear_failure(db, receipts, monkeypatch):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_owner")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    caller = registry_module.ProcessRegistry()
    session = caller.get("proc_owner")
    session.parent_session_id = "foreign"
    results.record_consumed(session)
    assert [f["id"] for f in helpers.refresh(db)["failures"]] == ["process:proc_owner"]


def test_unreadable_ack_keeps_unknown_coverage_and_prior_failure(db, receipts):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_badack")
    assert helpers.refresh(db)["failures"]
    (receipts / "proc_badack.ack").write_text("not-json")
    status = helpers.refresh(db)
    assert status["coverage"]["state"] == "unknown"
    assert [f["id"] for f in status["failures"]] == ["process:proc_badack"]


def test_expired_ack_is_pruned_with_the_result(db, receipts, monkeypatch):
    import os
    import time
    path = receipt(receipts, pid="proc_expired")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    caller = registry_module.ProcessRegistry()
    assert caller.poll("proc_expired", acknowledge=True)["status"] == "exited"
    ack = path.with_suffix(".ack")
    old = time.time() - results.RESULT_RETENTION_SECONDS - 1
    os.utime(path, (old, old))
    os.utime(ack, (old, old))
    results._result_paths()
    assert not path.exists() and not ack.exists()


def test_acknowledgement_from_another_interpreter_clears_retained_failure(db, receipts):
    import os
    import subprocess
    import sys
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_subprocess")
    assert helpers.refresh(db)["failures"]
    env = {**os.environ, "HERMES_HOME": str(receipts.parent.parent),
           "HERMES_SESSION_ID": "chat", "PYTHONPATH": os.pathsep.join(sys.path)}
    completed = subprocess.run([sys.executable, "-c", "from tools.process_registry import ProcessRegistry; "
                               "assert ProcessRegistry().poll('proc_subprocess', acknowledge=True)['status'] == 'exited'"],
                              env=env, capture_output=True, text=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
    assert helpers.refresh(db)["failures"] == []


def test_hosted_ack_never_writes_host_state(db, receipts, monkeypatch):
    receipt(receipts, pid="proc_hosted")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    session = registry_module.ProcessRegistry().get("proc_hosted")
    assert session is not None
    session.sandbox_key = "hosted-fixture"
    results.record_consumed(session)
    assert not (receipts / "proc_hosted.ack").exists()


def test_failed_ack_write_does_not_fabricate_a_durable_ack(db, receipts, monkeypatch, caplog):
    helpers.turn(db)
    receipt(receipts, exit_code=1, pid="proc_writefail")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    def failed_write(*args, **kwargs):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(results, "atomic_json_write", failed_write)
    assert registry_module.ProcessRegistry().poll("proc_writefail", acknowledge=True)["status"] == "exited"
    assert "Could not retain process acknowledgement" in caplog.text
    assert [f["id"] for f in helpers.refresh(db)["failures"]] == ["process:proc_writefail"]


def test_orphan_ack_expires_but_a_live_receipts_ack_stays(db, receipts, monkeypatch):
    import os
    import time
    path = receipt(receipts, pid="proc_oldack")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat")
    assert registry_module.ProcessRegistry().poll("proc_oldack", acknowledge=True)["status"] == "exited"
    ack = path.with_suffix(".ack")
    old = time.time() - results.RESULT_RETENTION_SECONDS - 1
    os.utime(ack, (old, old))
    results._result_paths()
    assert ack.exists()
    path.unlink()
    results._result_paths()
    assert not ack.exists()

