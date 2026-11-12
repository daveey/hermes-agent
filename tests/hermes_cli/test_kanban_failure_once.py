"""Regression coverage for failure notices from overlapping run observers."""

import pytest

from hermes_cli.kanban_failure_summary import describe_failure
from types import SimpleNamespace


@pytest.mark.parametrize("kind", ["crashed", "timed_out", "gave_up"])
def test_canonical_protocol_suppresses_provisional_desktop_notice(kind):
    from tui_gateway.session_notifications import _format_kanban_event_text
    event = SimpleNamespace(kind=kind, payload={"failure_notice": "canonical", "retry_status": "ready"})
    assert _format_kanban_event_text({"task_id": "fixture"}, None, event, "default") is None


def test_canonical_desktop_notice_is_rendered_with_final_status():
    from tui_gateway.session_notifications import _format_kanban_event_text
    event = SimpleNamespace(kind="failure_finalized", payload={
        "run_uid": "run-fixture", "final_status": "triage", "tripped": True,
        "failures": 3, "effective_limit": 2, "retry_status": "ready"})
    text = _format_kanban_event_text({"task_id": "fixture"}, None, event, "default")
    assert text and "triage" in text and "2 of 2" in text
    assert "returned to ready" not in text


@pytest.mark.parametrize("kind", ["crashed", "timed_out", "gave_up"])
def test_legacy_failure_notices_remain_visible(kind):
    from tui_gateway.session_notifications import _format_kanban_event_text
    event = SimpleNamespace(kind=kind, payload={"retry_status": "ready"})
    assert _format_kanban_event_text({"task_id": "fixture"}, None, event, "default")


def test_final_event_semantic_key_and_diagnostic_classification():
    from hermes_cli.kanban_failure_notice import event_key
    from gateway.kanban_watchers_notifier import diagnostic_event
    first = SimpleNamespace(id=1, kind="failure_finalized", payload={"run_uid": "run-a"})
    replay = SimpleNamespace(id=2, kind="failure_finalized", payload={"run_uid": "run-a"})
    next_run = SimpleNamespace(id=3, kind="failure_finalized", payload={"run_uid": "run-b"})
    assert event_key("default", first) == event_key("default", replay)
    assert event_key("default", first) != event_key("default", next_run)
    assert event_key("default", first) != event_key("other", first)
    assert diagnostic_event(first)
    assert event_key("default", SimpleNamespace(id=4, kind="crashed", payload={})) == "default:4"
    assert event_key("default", SimpleNamespace(id=5, kind="failure_finalized", payload={})) == "default:5"


@pytest.mark.parametrize("failures,limit", [(3, 2), (9, 1), (2, 2)])
def test_notice_never_displays_more_attempts_than_budget(failures, limit):
    text = describe_failure("gave_up", "fixture", {
        "failures": failures,
        "effective_limit": limit,
        "trigger_outcome": "crashed",
        "retry_status": "triage",
    })
    assert f"{min(failures, limit)} of {limit} failed attempt(s)" in text


@pytest.mark.parametrize("status", ["ready", "review", "triage"])
def test_final_notice_uses_authoritative_status_not_retry_phase(status):
    text = describe_failure("failure_finalized", "fixture", {
        "failures": 3, "effective_limit": 2, "trigger_outcome": "crashed",
        "retry_status": "ready", "final_status": status, "tripped": status == "triage",
    })
    if status == "triage":
        assert "2 of 2 failed attempt(s)" in text
        assert "triage" in text
        assert "returned to ready" not in text
        assert "Blocked" not in text
        assert "no user action" in text
    else:
        assert f"returned to {status}" in text
