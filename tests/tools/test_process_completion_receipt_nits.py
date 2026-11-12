"""Bounded malformed custody, retrieval visibility and serialized kill settlement."""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools import process_registry_obligations as ob
from tools import process_registry_results as results
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_ID", "parent")

    def make(sid="proc_aaaaaaaaaaaa"):
        session = ProcessSession(id=sid, command="true", task_id="t", parent_session_id="parent",
                                 session_key="agent:main:local:dm:x", started_at=time.time(),
                                 notify_on_complete=True, watcher_platform="local", watcher_chat_id="x",
                                 watcher_interval=5, exited=True, exit_code=0)
        results.save_completed_result(session, ob.obligation_for(session))
        return session, ob.receipt_path(sid)
    return make


@pytest.mark.parametrize("budget", ["age", "starts", "bad_age", "bad_starts", "nan_age", "inf_starts"])
def test_malformed_receipt_is_bounded_without_stopping_other_recovery(receipt, budget):
    _, path = receipt()
    valid, _ = receipt("proc_bbbbbbbbbbbb")
    record = json.loads(path.read_text())
    del record["exit_code"]
    owed = record[ob.OBLIGATION_KEY]
    if budget == "age":
        owed["owed_since"] = time.time() - ob.OBLIGATION_MAX_AGE_SECONDS - 1
    elif budget == "starts":
        owed["recoveries"] = ob.OBLIGATION_MAX_RECOVERIES
    else:
        key, value = {"bad_age": ("owed_since", "bad"), "bad_starts": ("recoveries", "bad"),
                      "nan_age": ("owed_since", float("nan")),
                      "inf_starts": ("recoveries", float("inf"))}[budget]
        owed[key] = value
    path.write_text(json.dumps(record))
    registry = ProcessRegistry()
    registry.recover_from_checkpoint(owner="gateway")
    assert ob.OBLIGATION_KEY not in json.loads(path.read_text())
    assert [w["session_id"] for w in registry.pending_watchers] == [valid.id]


def test_failed_reconstruction_spends_start_budget(receipt):
    _, path = receipt()
    record = json.loads(path.read_text())
    del record["exit_code"]
    path.write_text(json.dumps(record))
    for _ in range(ob.OBLIGATION_MAX_RECOVERIES + 1):
        ProcessRegistry().recover_from_checkpoint(owner="gateway")
    assert ob.OBLIGATION_KEY not in json.loads(path.read_text())


def test_retained_over_cap_owed_receipt_is_retrievable(receipt, monkeypatch):
    session, path = receipt()
    old = time.time() - 60
    os.utime(path, (old, old))
    monkeypatch.setattr(results, "MAX_RETAINED_RESULTS", 1)
    receipt("proc_bbbbbbbbbbbb")
    assert path.exists()
    assert session.id in results.load_completed_results(session.id)


def test_explicit_kill_rewrite_logs_settlement(receipt, caplog):
    session, path = receipt()
    with caplog.at_level(logging.INFO, logger="tools.process_registry"):
        ProcessRegistry()._rewrite_killed_receipt(session, consume_output=True)
    assert ob.OBLIGATION_KEY not in json.loads(path.read_text())
    assert f"Completion of {session.id} settled (consumed via kill)" in caplog.text


@pytest.mark.parametrize("gate", ["foreign_profile", "live_writer", "tui", None])
def test_bad_budget_preserves_non_owned_custody(receipt, monkeypatch, gate):
    _, path = receipt()
    record = json.loads(path.read_text())
    record[ob.OBLIGATION_KEY]["recoveries"] = "bad"
    registry = ProcessRegistry()
    if gate == "foreign_profile":
        record["session_key"] = "agent:other:local:dm:x"
        registry.profile_scoped_checkpoints = True
    elif gate == "live_writer":
        record[ob.OBLIGATION_KEY]["writer_pid"] = -1
        monkeypatch.setattr(registry, "_writer_alive", lambda owed: True)
    before = json.dumps(record)
    path.write_text(before)
    registry.recover_from_checkpoint(owner="gateway" if gate in ("foreign_profile", "live_writer") else gate)
    assert path.read_text() == before
    assert registry.pending_watchers == []


@pytest.mark.parametrize("first", ["kill", "settle"])
def test_settlement_and_kill_rewrite_serialize(receipt, monkeypatch, first):
    session, path = receipt()
    registry = ProcessRegistry()
    entered, release, contended = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if lock.locked():
                contended.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    registry._lock = ObservedLock()
    original = ob.owed_still if first == "kill" else ob._rewrite

    def pause(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(ob, "owed_still" if first == "kill" else "_rewrite", pause)
    kill = lambda: registry._rewrite_killed_receipt(session, consume_output=False)
    settle = lambda: registry.settle_completion(session.id, "admitted")
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(kill if first == "kill" else settle)
        try:
            assert entered.wait(5)
            two = pool.submit(settle if first == "kill" else kill)
            assert contended.wait(5), "competing receipt writer did not take the registry lock"
        finally:
            release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert ob.OBLIGATION_KEY not in json.loads(path.read_text())
