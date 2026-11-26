"""Queued CI is a producer wait, not owner/job progress or a bare scheduling declaration."""
import pytest

from tests.hermes_state.test_session_status_refresh import (
    board as board,  # noqa: PLC0414 -- pytest fixture reexport
    db as db,  # noqa: PLC0414 -- pytest fixture reexport
    receipts as receipts,  # noqa: PLC0414 -- pytest fixture reexport
    refresh,
    turn,
)


@pytest.mark.parametrize("kind", ["ci", "deploy"])
@pytest.mark.parametrize("remote", [True, False])
def test_confirmed_queued_job_is_a_wait_not_progress(db, monkeypatch, kind, remote):
    from hermes_cli import kanban_session_activity as activity
    import hermes_state_outcome as outcome

    turn(db)
    card = {"id": "queued", "status": "scheduled", "remote": remote,
            "wait": {"wait_kind": kind, "wait_state": "external_running", "external": {"job": "queued"}}}
    monkeypatch.setattr(activity, "session_outcome_tasks", lambda *args, **kwargs: [card])
    value = refresh(db, "card_change")
    assert value["kind"] == "waiting"
    assert value["presentation"]["stripe"] == "external"
    assert value["on"][0]["state"] == "producer_wait"
    assert outcome.get_outcome("chat", db=db)["status"] == value
    card["wait"]["external"]["job"] = "running"
    value = refresh(db, "card_change")
    assert value["on"][0]["state"] == "external_running"
    assert value["kind"] == "waiting"


@pytest.mark.parametrize("change", [
    {"stale": True}, {"partial_source_unknown": "offline"},
    {"wait": {"wait_kind": "ci", "wait_state": "external_unknown", "external": {"job": "queued"}}},
    {"wait": {"wait_kind": "clock", "wait_state": "external_running", "external": {"job": "queued"}}},
    {"wait": {"wait_kind": "ci"}},
])
def test_unconfirmed_queued_job_never_becomes_a_blue_producer(db, monkeypatch, change):
    from hermes_cli import kanban_session_activity as activity

    turn(db)
    card = {"id": "queued", "status": "scheduled",
            "wait": {"wait_kind": "ci", "wait_state": "external_running", "external": {"job": "queued"}}, **change}
    monkeypatch.setattr(activity, "session_outcome_tasks", lambda *args, **kwargs: [card])
    value = refresh(db, "card_change")
    assert value["kind"] == "waiting"
    assert value["presentation"]["stripe"] != "external"
