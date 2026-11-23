"""Delegated compression tips govern terminal reconciliation and acknowledgment."""
import json

import pytest

from tests.hermes_state import test_subagent_status_resolution as fixtures
from hermes_state_subagent_resolution import acknowledge_subagents
from tools.delegate_tool_sessions import stored_child_session

board, db, receipts, ledger = fixtures.board, fixtures.db, fixtures.receipts, fixtures.ledger
child, delivered, refresh, turn = fixtures.child, fixtures.delivered, fixtures.refresh, fixtures.turn


def rotate(db):
    marker = {"_delegate_from": "chat", "_subagent_id": "sa_review"}
    db._write_sql("UPDATE sessions SET model_config=?, ended_at=NULL, end_reason=NULL WHERE id=?",
                  (json.dumps(marker), "child"))
    db.end_session("child", "compression")
    db.create_session("child-tip", source="subagent", parent_session_id="child", model_config=marker)
    assert stored_child_session(db, "sa_review", ["chat"]) == "child-tip"
    return marker


def test_late_completed_rotated_child_clears_retained_loss(db):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    rotate(db)
    db.append_message("child-tip", role="user", content="finish")
    db.append_message("child-tip", role="assistant", content="verified done")
    db.end_session("child-tip", "agent_close")
    assert refresh(db)["failures"] == []


def test_explicit_ack_refuses_live_rotated_child(db):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    rotate(db)
    db.append_message("child-tip", role="user", content="continue work")
    with pytest.raises(ValueError, match="still live"):
        acknowledge_subagents(db, "chat", ["child"], "verified earlier work")
    assert db._read_one("SELECT value FROM state_meta WHERE key='subagent_ack:child'") is None


@pytest.mark.parametrize("receipt", ["explicit", "delivered"])
def test_old_receipt_does_not_cover_new_live_continuation(db, receipt):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    if receipt == "explicit":
        acknowledge_subagents(db, "chat", ["child"], "verified earlier work")
    else:
        delivered(db)
    assert refresh(db)["failures"] == []
    rotate(db)
    from hermes_state_subagent_resolution import resolved_subagents
    assert "subagent:child" not in resolved_subagents(db, ["chat"], [])


@pytest.mark.parametrize("marker", ["_branched_from", "_reset_from", "_delegate_from"])
def test_completed_fork_cannot_resolve_compressed_child(db, marker):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    rotate(db)
    db._write_sql("UPDATE sessions SET model_config=? WHERE id='child-tip'",
                  (json.dumps({marker: "child"}),))
    db.append_message("child-tip", role="user", content="separate work")
    db.append_message("child-tip", role="assistant", content="done")
    db.end_session("child-tip", "agent_close")
    assert refresh(db)["failures"]


def test_multiple_rotations_resolve_only_at_terminal_tip(db):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    marker = rotate(db)
    db.end_session("child-tip", "compression")
    db.create_session("child-tip-2", source="subagent", parent_session_id="child-tip", model_config=marker)
    with pytest.raises(ValueError, match="still live"):
        acknowledge_subagents(db, "chat", ["child"], "earlier work")
    db.append_message("child-tip-2", role="user", content="finish")
    db.append_message("child-tip-2", role="assistant", content="done")
    db.end_session("child-tip-2", "agent_close")
    assert refresh(db)["failures"] == []


def test_explicit_ack_of_ended_continuation_covers_tip_activity(db):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    rotate(db)
    db.append_message("child-tip", role="user", content="work")
    db.end_session("child-tip", "delegation_unknown")
    acknowledge_subagents(db, "chat", ["child"], "independently verified successor work")
    assert refresh(db)["failures"] == []
