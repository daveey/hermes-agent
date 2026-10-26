"""Held final candidates stay private on progress and terminal result channels."""
from tests.agent.test_pre_final_gate import Run, _buffered, _run
from tests.agent import test_pre_final_gate as support

agent = support.agent


def test_held_candidate_never_relayed_as_reasoning(agent):
    run = Run(agent, ["Rejected private diagnosis.", "Verified accepted response."])
    progress = []
    agent.tool_progress_callback = lambda *args: progress.append(args)

    @_buffered
    def gate(attempt, **kwargs):
        return {"hold": True, "nudge": "Continue authorized work."} if attempt == 0 else None

    result = _run(agent, gate)
    assert result["completed"]
    assert "Rejected private diagnosis." not in str(progress)
    assert "Verified accepted response." in run.shown


def test_unresolved_gate_is_error_for_delivery_adapters(agent):
    Run(agent, ["Rejected private diagnosis."] * 3)

    @_buffered
    def gate(**kwargs):
        return {"hold": True, "nudge": "Continue authorized work.",
                "unresolved_fallback": {"goal": "Repair example", "evidence_refs": ["user:0"]}}

    result = _run(agent, gate, "Repair example")
    assert not result["completed"]
    assert result["failed"]
    assert result["error"]
    assert result["failure_reason"] == "pre_final_gate_unresolved"
    assert result["failure_retryable"] is False
    assert "Rejected private diagnosis." not in result["error"]


def test_unresolved_error_surface_names_runtime_not_provider():
    from agent.error_surface import build_error_surface_from_result
    result = {"error": "Goal remains unresolved", "failure_reason": "pre_final_gate_unresolved"}
    assert build_error_surface_from_result(result) == {
        "layer": "gateway", "code": "pre_final_gate_unresolved", "retryable": False}


def test_unbuffered_progress_preview_unchanged(agent):
    Run(agent, ["Ordinary response."])
    progress = []
    agent.tool_progress_callback = lambda *args: progress.append(args)
    result = _run(agent, lambda **kwargs: None)
    assert result["completed"]
    assert "Ordinary response." in str(progress)
