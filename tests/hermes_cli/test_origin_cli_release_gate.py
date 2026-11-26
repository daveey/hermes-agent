"""Release admission reports are not creation or execution receipts."""
import pytest

from hermes_cli.kanban_origin_admission import AdmissionRefused
from hermes_cli.kanban_origin_cli_gate import LaunchGate


@pytest.mark.parametrize("stage", [
    "intent_journaled_unsent", "authorized_pending_home", "read_first_pending", "consumed_or_applied", "applied",
])
def test_release_stage_is_preserved_without_claiming_creation(stage):
    launch = LaunchGate()
    request = launch.dispatch(operation="release")
    request.admit("target", intent_uid="intent-a")
    result = {"ok": True, "task_id": "target", "intent_uid": "intent-a", "release_stage": stage}
    assert request.finish(result) == {
        "operation": "release", "outcome": stage, "task_id": "target", "intent_uid": "intent-a", "finished": True,
    }
    assert launch.retire()[0]["outcome"] == stage


def test_create_contract_is_unchanged():
    launch = LaunchGate()
    request = launch.dispatch()
    assert launch.retire() == [{"outcome": "no_board_commit", "task_id": None, "finished": False}]
    with pytest.raises(AdmissionRefused):
        request.admit("task")
    launch = LaunchGate()
    request = launch.dispatch()
    request.admit("task")
    assert request.finish({"ok": True, "task_id": "task"}) == {
        "outcome": "created", "task_id": "task", "finished": True,
    }


@pytest.mark.parametrize("result", [None, [], {},
    {"ok": True, "task_id": "target", "intent_uid": "wrong", "release_stage": "read_first_pending"},
    {"ok": True, "task_id": "wrong", "intent_uid": "intent-a", "release_stage": "read_first_pending"},
    {"ok": True, "task_id": "target", "intent_uid": "intent-a", "release_stage": "created"},
    {"ok": True, "task_id": "target", "intent_uid": "intent-a"},
    {"ok": 1, "task_id": "target", "intent_uid": "intent-a", "release_stage": "consumed_or_applied"},
    {"ok": False, "task_id": "target", "intent_uid": "intent-a", "release_stage": "consumed_or_applied"},
    {"ok": True, "task_id": "target", "intent_uid": "intent-a", "release_stage": []},
    {"ok": True, "task_id": "target", "intent_uid": "intent-a", "release_stage": {}},
])
def test_release_uncertain_reply_cannot_become_created_or_pending(result):
    request = LaunchGate().dispatch(operation="release")
    request.admit("target", intent_uid="intent-a")
    report = request.finish(result)
    assert report == {"operation": "release", "outcome": "outcome_unknown", "task_id": "target",
                      "intent_uid": "intent-a", "finished": True}


@pytest.mark.parametrize("operation", ["unknown", "", None, 1, [], {}])
def test_unknown_operation_does_not_gain_admission(operation):
    launch = LaunchGate()
    with pytest.raises((ValueError, TypeError)):
        launch.dispatch(operation=operation)
    assert launch.retire() == []


def test_release_retirement_before_journal_refuses_admission():
    launch = LaunchGate()
    request = launch.dispatch(operation="release")
    assert launch.retire() == [{"operation": "release", "outcome": "no_board_commit", "task_id": None,
                               "intent_uid": None, "finished": False}]
    with pytest.raises(AdmissionRefused):
        request.admit("target", intent_uid="intent-a")


def test_release_retirement_after_admission_does_not_claim_rollback():
    launch = LaunchGate()
    request = launch.dispatch(operation="release")
    request.admit("target", intent_uid="intent-a")
    assert launch.retire()[0]["outcome"] == "outcome_unknown"
    with pytest.raises(AdmissionRefused):
        request.admit("target", intent_uid="intent-b")
    assert request.finish({"ok": True, "task_id": "target", "intent_uid": "intent-a",
                           "release_stage": "read_first_pending"})["outcome"] == "read_first_pending"
    with pytest.raises(AdmissionRefused):
        request.finish()


@pytest.mark.parametrize("intent", [None, "", 0, [], {}])
def test_release_requires_exact_intent_before_journal_admission(intent):
    launch = LaunchGate()
    request = launch.dispatch(operation="release")
    with pytest.raises(AdmissionRefused):
        request.admit("target", intent_uid=intent)
    assert request.finish()["outcome"] == "no_board_commit"


def test_release_success_without_admission_does_not_prove_commit():
    request = LaunchGate().dispatch(operation="release")
    assert request.finish({"ok": True, "task_id": "target", "intent_uid": "intent-a",
                           "release_stage": "consumed_or_applied"})["outcome"] == "no_board_commit"
