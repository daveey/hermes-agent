"""Admission is ordered against retirement, not confused with COMMIT."""
from contextlib import contextmanager
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_admission as admission, kanban_origin_native as native
from hermes_cli.kanban_origin_cli_gate import LaunchGate
from tests.hermes_cli import test_origin_native_admission as fixtures

turn = fixtures.turn


@pytest.mark.parametrize("when", ["before", "after"])
def test_retirement_at_final_admission(turn, monkeypatch, when):
    launch = LaunchGate()
    request = launch.dispatch()
    admit = request.admit
    def interleaving(task_id):
        if when == "before":
            report = launch.retire()
            assert report[0]["outcome"] == "no_board_commit"
        admit(task_id)
        if when == "after":
            report = launch.retire()
            assert report[0]["outcome"] == "outcome_unknown"
            assert not report[0]["finished"]
    monkeypatch.setattr(request, "admit", interleaving)
    with kbc.connect_closing() as conn:
        before = fixtures.snapshot(conn)
        with admission.strict_local_creation(gate=request):
            if when == "before":
                with pytest.raises(admission.AdmissionRefused):
                    kb.create_task(conn, title="retirement", native_origin=native.capture(None))
                assert fixtures.snapshot(conn) == before
            else:
                task = kb.create_task(conn, title="retirement", native_origin=native.capture(None))
                assert kb.get_task(conn, task) is not None
                assert request.finish({"ok": True, "task_id": task})["outcome"] == "created"


def test_retired_idempotent_replay_refused(turn):
    with kbc.connect_closing() as conn:
        captured = native.capture(None)
        kb.create_task(conn, title="existing", idempotency_key="gate-key", native_origin=captured)
        launch = LaunchGate()
        request = launch.dispatch()
        launch.retire()
        with admission.strict_local_creation(gate=request), pytest.raises(admission.AdmissionRefused):
            kb.create_task(conn, title="replay", idempotency_key="gate-key", native_origin=captured)


@pytest.mark.parametrize("error", [RuntimeError, sqlite3.IntegrityError])
def test_postcommit_exception_is_unknown_and_never_readmitted(turn, monkeypatch, error):
    launch = LaunchGate()
    request = launch.dispatch()
    original = kb.write_txn
    @contextmanager
    def postcommit(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        raise error("injected after COMMIT")
    monkeypatch.setattr(kb, "write_txn", postcommit)
    with kbc.connect_closing() as conn:
        with admission.strict_local_creation(gate=request), pytest.raises((error, admission.AdmissionRefused)):
            kb.create_task(conn, title="postcommit", native_origin=native.capture(None))
        report = request.finish()
        assert report["outcome"] == "outcome_unknown"
        assert kb.get_task(conn, report["task_id"]) is not None
        assert conn.execute("SELECT count(*) FROM tasks WHERE title='postcommit'").fetchone()[0] == 1


def test_gate_refuses_dispatch_after_retire():
    launch = LaunchGate()
    assert launch.retire() == []
    with pytest.raises(admission.AdmissionRefused):
        launch.dispatch()


@pytest.mark.parametrize("state", ["admitted", "finished", "retired"])
def test_gate_single_admission(state):
    launch = LaunchGate()
    request = launch.dispatch()
    if state == "admitted":
        request.admit("first")
    elif state == "finished":
        request.finish()
    else:
        launch.retire()
    with pytest.raises(admission.AdmissionRefused):
        request.admit("second")


@pytest.mark.parametrize("reply", [None, [], {}, {"ok": True, "task_id": "wrong"},
                                   {"ok": False, "task_id": "task"}, {"ok": 1, "task_id": "task"}])
def test_admitted_unverified_reply_is_unknown(reply):
    launch = LaunchGate()
    request = launch.dispatch()
    request.admit("task")
    assert request.finish(reply)["outcome"] == "outcome_unknown"
    assert launch.retire()[0]["outcome"] == "outcome_unknown"


@pytest.mark.parametrize("task_id", ["task", None])
def test_success_without_admission_is_not_created(task_id):
    launch = LaunchGate()
    request = launch.dispatch()
    assert request.finish({"ok": True, "task_id": task_id})["outcome"] == "no_board_commit"
    with pytest.raises(admission.AdmissionRefused):
        request.finish()
