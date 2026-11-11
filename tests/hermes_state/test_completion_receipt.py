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


@pytest.mark.parametrize("next_state", ["new_turn", "running", "muted", "decision"])
def test_historical_receipt_survives_new_current_state(db, monkeypatch, next_state):
    import hermes_state_outcome as so

    fixtures.turn(db)
    value = receipt("rejected")
    declared = so.declare_outcome("chat", {"kind": "done", "summary": "Assessment complete",
                                         "completion_receipt": value}, db=db)
    if next_state == "decision":
        so.declare_outcome("chat", {"kind": "decision", "items": [{"question": "Next?", "choices": ["yes", "no"]}]}, db=db)
    else:
        fixtures.say(db, "user", "New unrelated work")
    monkeypatch.setattr(so, "_live_turn", lambda *_: next_state == "running")
    if next_state == "muted":
        out = so.set_muted("chat", True, db=db)
    else:
        out = so.refresh_outcome("chat", "manual", db=db)
    if next_state != "decision":
        assert out["kind"] != "done"
    assert out.get("completion_receipt") == value
    assert out["completion_receipt_source"]["declared_at"] == declared["at"]
    assert so.get_outcome("chat", db=db)["completion_receipt"] == value


def test_malformed_declaration_leaves_existing_outcome_unchanged(db):
    import hermes_state_outcome as so

    fixtures.turn(db)
    so.declare_outcome("chat", {"kind": "done", "summary": "Prior result"}, db=db)
    before = so.get_outcome("chat", db=db)
    with pytest.raises(ValueError, match="completion_receipt"):
        so.declare_outcome("chat", {"kind": "done", "summary": "Invalid replacement",
                                   "completion_receipt": {"version": 1, "items": []}}, db=db)
    assert so.get_outcome("chat", db=db) == before


@pytest.mark.parametrize("rows", [[], [{"id": "t_owner", "board": "default", "gateway": "away",
                                      "status": "running", "stale": True}],
                                  [{"id": "t_owner", "board": "other", "gateway": "away", "status": "running"}],
                                  [{"id": "t_owner", "board": "default", "gateway": "here", "status": "running"}]])
def test_declaration_rejects_unverified_pending_owner_without_write(db, monkeypatch, rows):
    import hermes_state_outcome as so
    from hermes_cli import kanban_origin, kanban_session_activity

    fixtures.turn(db)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    monkeypatch.setattr(kanban_session_activity, "session_tasks", lambda *_args, **_kw: rows)
    value = receipt("contained")
    value["items"][0]["remaining"] = [{"scope": "cause", "state": "pending",
                                       "owner": "@task:default@away/t_owner", "reason": "follow-up"}]
    before = so.get_outcome("chat", db=db)
    with pytest.raises(ValueError, match="completion_receipt"):
        so.declare_outcome("chat", {"kind": "done", "summary": "Containment complete",
                                   "completion_receipt": value}, db=db)
    assert so.get_outcome("chat", db=db) == before


def test_declaration_accepts_existing_linked_local_owner(db, board, monkeypatch):
    import hermes_state_outcome as so
    from hermes_cli import kanban_origin

    fixtures.turn(db)
    fixtures.add_card(board, "t_owner", "chat", "running")
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here"}))
    value = receipt("contained")
    value["items"][0]["remaining"] = [{"scope": "cause", "state": "pending",
                                       "owner": "@task:default@here/t_owner", "reason": "follow-up"}]
    out = so.declare_outcome("chat", {"kind": "done", "summary": "Containment complete",
                                    "completion_receipt": value}, db=db)
    assert out["completion_receipt"] == value
    assert so.get_outcome("chat", db=db)["completion_receipt"] == value


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


def test_sidebar_rows_and_cli_json_project_receipt_and_source(db, monkeypatch, capsys):
    """The Desktop list API rows and `hermes sessions outcome --json` carry the stored receipt verbatim."""
    import argparse
    import json

    import hermes_state_outcome as so
    from hermes_cli import sessions_cmd_outcome as cli

    monkeypatch.setattr(so, "_live_turn", lambda *_: False)
    fixtures.turn(db)
    value = receipt("superseded")
    so.declare_outcome("chat", {"kind": "done", "summary": "Migration superseded.",
                                "completion_receipt": value}, db=db)
    row = next(r for r in db.list_sessions_rich(order_by_last_active=True, include_pinned=True,
                                                compact_rows=True) if r["id"] == "chat")
    assert row["outcome"]["completion_receipt"] == value
    assert row["outcome"]["completion_receipt_source"]["kind"] == "session_declaration"
    cli.cmd_outcome(db, argparse.Namespace(session_id="chat", refresh=False, json=True))
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["completion_receipt"] == value
    assert printed["outcome"]["completion_receipt_source"] == row["outcome"]["completion_receipt_source"]


@pytest.mark.parametrize("later", ["declared", "derived"])
def test_later_plain_done_does_not_inherit_earlier_receipt(db, monkeypatch, later):
    """A receipt belongs to the declaration that made it, never to a later done."""
    import hermes_state_outcome as so

    monkeypatch.setattr(so, "_live_turn", lambda *_: False)
    fixtures.turn(db)
    so.declare_outcome("chat", {"kind": "done", "summary": "A delivered.",
                                "completion_receipt": receipt("delivered")}, db=db)
    fixtures.say(db, "user", "Now do B")
    if later == "declared":
        out = so.declare_outcome("chat", {"kind": "done", "summary": "B plain"}, db=db)
    else:
        fixtures.say(db, "assistant", fixtures.SUMMARY)
        out = so.refresh_outcome("chat", "manual", db=db)
    assert out["kind"] == "done"
    assert "completion_receipt" not in out and "completion_receipt_source" not in out
    stored = so.get_outcome("chat", db=db)
    assert "completion_receipt" not in stored
    row = next(r for r in db.list_sessions_rich(limit=5) if r["id"] == "chat")
    assert "completion_receipt" not in row["outcome"]
