"""Status reads must distinguish expiry from unavailable current evidence."""
import json
import os
import time

import pytest

from tools import process_registry_results as results
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "get_hermes_home", lambda: tmp_path)
    directory = tmp_path / "logs" / "process-results"
    directory.mkdir(parents=True)
    return directory


def record(directory, exit_code=0):
    data = dict(id="proc_example", command="true", cwd="/", task_id="task",
                owner_task_id="task", session_key="", parent_session_id="owner",
                started_at=time.time(), exit_code=exit_code, completion_reason="exited",
                termination_source="", notify_on_complete=False, output="")
    path = directory / "proc_example.json"
    path.write_text(json.dumps(data))
    return path


def test_enumeration_failure(receipts, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("inventory unavailable")
    monkeypatch.setattr(results, "_result_paths", fail)
    with pytest.raises(OSError):
        results.load_completed_results(strict=True, owner_session_id="owner")


def test_malformed_retained_record(receipts):
    (receipts / "proc_bad.json").write_text("{")
    with pytest.raises(ValueError):
        results.load_completed_results(strict=True, owner_session_id="owner")


def test_current_missing_exit_evidence(receipts):
    record(receipts, None)
    with pytest.raises(ValueError):
        results.load_completed_results(strict=True, owner_session_id="owner")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_retained_terminal_evidence(receipts, exit_code):
    record(receipts, exit_code)
    loaded = results.load_completed_results(strict=True, owner_session_id="owner")
    assert loaded["proc_example"].exit_code == exit_code
    assert loaded["proc_example"].exited


def test_genuine_expiry(receipts):
    path = record(receipts, 7)
    expired = time.time() - results.RESULT_RETENTION_SECONDS - 60
    os.utime(path, (expired, expired))
    assert results.load_completed_results(strict=True, owner_session_id="owner") == {}


def test_strict_requires_explicit_owner(receipts):
    with pytest.raises(ValueError):
        results.load_completed_results(strict=True)


@pytest.mark.parametrize("exited,exit_code", [(False, None), (True, 0), (True, 7)])
def test_qualified_registry_list(receipts, monkeypatch, exited, exit_code):
    registry = ProcessRegistry()
    own = ProcessSession(id="proc_owned", command="true", cwd="/", task_id="task",
                         parent_session_id="owner", exited=exited, exit_code=exit_code)
    foreign = ProcessSession(id="proc_foreign", command="true", cwd="/", task_id="other",
                             parent_session_id="foreign", exited=True, exit_code=None)
    registry._running = {own.id: own, foreign.id: foreign}
    monkeypatch.setattr(results, "_owns_result", lambda owner, parent: owner == parent)
    monkeypatch.setattr(registry, "_refresh_detached_session", lambda s: s)
    monkeypatch.setattr(registry, "_reconcile_local_exit", lambda s: None)
    rows = registry.list_sessions(include_retained=True, strict=True, owner_session_id="owner")
    assert [row["session_id"] for row in rows] == [own.id]
    assert rows[0]["status"] == ("exited" if exited else "running")
    if exited:
        assert rows[0]["exit_code"] == exit_code


def test_hidden_uncollected_record_is_unknown(receipts, monkeypatch):
    registry = ProcessRegistry()
    own = ProcessSession(id="proc_owned", command="true", cwd="/", task_id="task",
                         parent_session_id="owner", exited=True, exit_code=None)
    registry._finished = {own.id: own}
    monkeypatch.setattr(registry, "_refresh_detached_session", lambda s: s)
    monkeypatch.setattr(registry, "_uncollected_gone", lambda s: True)
    with pytest.raises(ValueError, match="exit evidence"):
        registry.list_sessions(include_retained=True, strict=True, owner_session_id="owner")


def test_real_enumeration_error_propagates(receipts, monkeypatch):
    def fail(self):
        raise PermissionError("cannot enumerate")
    monkeypatch.setattr(type(receipts), "iterdir", fail)
    with pytest.raises(PermissionError):
        results.load_completed_results(strict=True, owner_session_id="owner")


def test_record_pruned_between_enumeration_and_read_is_expiry(receipts, monkeypatch):
    """Another producer's prune (retention or cap) can remove a receipt after it was listed: that is
    expiry under the existing retention rule, not unreadable current evidence."""
    kept = record(receipts, 7)
    gone = receipts / "proc_gone.json"
    monkeypatch.setattr(results, "_result_paths", lambda **kw: [gone, kept])
    loaded = results.load_completed_results(strict=True, owner_session_id="owner")
    assert set(loaded) == {"proc_example"}


def test_record_pruned_during_enumeration_is_expiry(receipts, monkeypatch):
    kept = record(receipts, 0)
    real = type(receipts).iterdir
    monkeypatch.setattr(type(receipts), "iterdir",
                        lambda self: [*real(self), self / "proc_gone.json"] if self == receipts else real(self))
    assert results._result_paths(strict=True) == [kept]
