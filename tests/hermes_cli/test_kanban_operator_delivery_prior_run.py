"""Delivery belongs to the requesting task, not only its currently running attempt."""
import json

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_operator_delivery import _delivered, _receipt
from tests.hermes_cli import test_kanban_operator_call as helpers

fleet = helpers.fleet


def _resume(f):
    assert kb.schedule_task(f.w, f.caller, expected_run_id=f.run_id,
                            reason="Wait for cooldown", clock_only=True,
                            awaiting={"kind": "clock", "why": "cooldown"})
    assert kb.unblock_task(f.w, f.caller)
    assert kb.claim_task(f.w, f.caller)
    f.run_id = kb._current_run_id(f.w, f.caller)
    f.run_uid(f.w, f.caller)


@pytest.mark.parametrize("moved", [False, True, "no_ledger_table"])
def test_prior_run_delivery_completes_after_resume(fleet, moved):
    f = fleet
    proof = _delivered(f)
    _resume(f)
    if moved:
        # Destination has task/run history but no source-local operator ledger.
        f.gw[id(f.w)] = "worker-9"
        with kb.write_txn(f.w):
            f.w.execute("DELETE FROM operator_calls")
            if moved == "no_ledger_table":
                f.w.execute("DROP TABLE operator_calls")
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id,
                            summary=f"Posted findings to {f.target}.", metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "done"


@pytest.mark.parametrize("change", ["other_task", "other_run", "missing_request", "canonical_actor"])
def test_resumed_receipt_rejects_mismatches(fleet, change):
    f = fleet
    proof = _delivered(f)
    _resume(f)
    if change == "other_task":
        proof["caller_task_id"] = f.target
    elif change == "other_run":
        proof["caller_run_uid"] = "forged-run"
    elif change == "missing_request":
        # Missing ledger alone is recoverable on the same host too. Remove the
        # carried locators as well to exercise truly missing request evidence.
        with kb.write_txn(f.w):
            f.w.execute("DELETE FROM operator_calls")
            f.w.execute("DELETE FROM task_events WHERE kind='operator_call_requested'")
        f.rep.execute("DELETE FROM task_events WHERE kind='operator_call_requested'")
        f.rep.commit()
    else:
        f.gw[id(f.w)] = "worker-9"
        with kb.write_txn(f.w):
            f.w.execute("DELETE FROM operator_calls")
        f.origin[proof["result_event_uid"]] = "impostor"
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("change", ["content", "run", "task", "board", "event_origin", "unreachable"])
def test_moved_receipt_still_requires_canonical_request(fleet, change):
    f = fleet
    proof = _delivered(f)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?",
                      (proof["request_uid"],)).fetchone()[0]
    _resume(f)
    f.gw[id(f.w)] = "worker-9"
    with kb.write_txn(f.w):
        f.w.execute("DELETE FROM operator_calls")
    request = json.loads(f.canonical[uid]["payload"])
    edits = {
        "content": (request["args"], "text", "forged"),
        "run": (request["caller"], "run_uid", "forged"),
        "task": (request["caller"], "task_id", f.target),
        "board": (request, "board", "other"),
        "event_origin": (f.origin, uid, "impostor"),
    }
    if change in edits:
        target, key, value = edits[change]
        target[key] = value
    else:
        f.evidence_down = True
    f.canonical[uid]["payload"] = json.dumps(request)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "running"
