"""Artifact acceptance is independent of workflow completion (anonymous fixtures)."""
from copy import deepcopy

import pytest

from hermes_state_outcome import validate_declaration
from tests.hermes_state import test_session_outcome as fixtures

board = fixtures.board
db = fixtures.db


def test_persisted_receipt_survives_refresh_and_reopen(db, monkeypatch):
    import hermes_state_outcome as so
    from hermes_state import SessionDB

    monkeypatch.setattr(so, "_live_turn", lambda *_: False)
    fixtures.turn(db)
    value = receipt("rejected")
    so.declare_outcome("chat", {"kind": "done", "summary": "Assessment finished; artifact rejected.",
                               "completion_receipt": value}, db=db)
    path = db.db_path
    db.close()
    reopened = SessionDB(db_path=path)
    try:
        assert so.get_outcome("chat", db=reopened)["completion_receipt"] == value
        assert so.refresh_outcome("chat", "manual", db=reopened)["completion_receipt"] == value
        assert so.refresh_outcome("chat", "manual", db=reopened)["completion_receipt"] == value
    finally:
        reopened.close()


def test_malformed_declaration_leaves_existing_outcome_unchanged(db):
    import hermes_state_outcome as so

    fixtures.turn(db)
    so.declare_outcome("chat", {"kind": "done", "summary": "Prior result"}, db=db)
    before = so.get_outcome("chat", db=db)
    with pytest.raises(ValueError, match="completion_receipt"):
        so.declare_outcome("chat", {"kind": "done", "summary": "Invalid replacement",
                                   "completion_receipt": {"version": 1, "items": []}}, db=db)
    assert so.get_outcome("chat", db=db) == before


def receipt(disposition="delivered"):
    item = {
        "scope": "bounded assessment artifact",
        "disposition": disposition,
        "evidence": [{"ref": "https://example.com/review/1", "observed_at": "2026-01-01T00:00:00Z",
                      "verification": "reported"}],
        "remaining": [],
    }
    if disposition == "superseded":
        item["superseded_by"] = "@task:default@other/t_successor"
    return {"version": 1, "items": [item]}


def declare(value):
    return validate_declaration({"kind": "done", "summary": "Assessment finished.",
                                 "completion_receipt": value})


@pytest.mark.parametrize("disposition", ["delivered", "rejected", "contained", "superseded"])
def test_workflow_done_does_not_imply_artifact_acceptance(disposition):
    value = receipt(disposition)
    out = declare(value)
    assert out["kind"] == "done"
    assert out["completion_receipt"] == value
    assert out["completion_receipt"] is not value


def test_report_delivered_does_not_authorize_deferred_enforcement():
    value = receipt()
    value["items"][0]["remaining"] = [{"scope": "enforcement", "state": "deferred", "owner": None,
                                       "reason": "User deferred; no execution authorized."}]
    assert declare(value)["completion_receipt"] == value


def test_mixed_superseded_migration_and_delivered_hardening():
    value = receipt("superseded")
    value["items"] += receipt()["items"]
    value["items"][1]["scope"] = "artifact hardening"
    assert [i["disposition"] for i in declare(value)["completion_receipt"]["items"]] == [
        "superseded", "delivered"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(version=True),
    lambda r: r.update(version=2),
    lambda r: r.update(items=[]),
    lambda r: r.update(extra="ignored would be unsafe"),
    lambda r: r["items"][0].update(scope=""),
    lambda r: r["items"][0].update(disposition="fixed"),
    lambda r: r["items"][0].update(evidence=[]),
    lambda r: r["items"][0]["evidence"][0].update(ref=""),
    lambda r: r["items"][0]["evidence"][0].update(observed_at="yesterday"),
    lambda r: r["items"][0]["evidence"][0].update(observed_at="2026-01-01T00:00:00"),
    lambda r: r["items"][0]["evidence"][0].update(verification="verified"),
    lambda r: r["items"][0].update(disposition="superseded"),
    lambda r: r["items"][0].update(superseded_by="unrelated"),
    lambda r: r["items"][0].update(remaining=[{"scope": "fix", "state": "pending", "owner": None,
                                             "reason": "someone should"}]),
    lambda r: r["items"][0].update(remaining=[{"scope": "fix", "state": "pending", "owner": "t_local",
                                             "reason": "ambiguous owner"}]),
    lambda r: r["items"][0].update(remaining=[{"scope": "fix", "state": "deferred", "owner": None,
                                             "reason": ""}]),
])
def test_malformed_receipt_is_rejected_not_stripped(mutate):
    value = receipt()
    mutate(value)
    before = deepcopy(value)
    with pytest.raises(ValueError, match="completion_receipt"):
        declare(value)
    assert value == before


def test_whole_receipt_size_is_bounded():
    value = receipt()
    item = value["items"][0]
    item["scope"] = "x" * 2000
    value["items"] = [deepcopy(item) for _ in range(32)]
    with pytest.raises(ValueError, match="32768"):
        declare(value)


def test_legacy_done_has_unspecified_artifact_acceptance():
    assert "completion_receipt" not in validate_declaration({"kind": "done", "summary": "Done"})


def test_receipt_is_not_allowed_on_decision_declaration():
    with pytest.raises(ValueError, match="completion_receipt"):
        validate_declaration({"kind": "decision", "items": [{"question": "Continue?", "choices": ["yes", "no"]}],
                              "completion_receipt": receipt()})
