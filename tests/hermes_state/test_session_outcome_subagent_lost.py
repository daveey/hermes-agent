"""A delegate child whose row never ended and that went silent past the live window is *lost*, not gone
(hermes_state_outcome): it must not age out of the parent's waits into a derived ``done`` (false green,
defect desktop-5a35611881). A declared outcome made after its last activity accounts for it."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB

# "no additional action is needed" read as a closing summary by the heuristic (the incident's reply shape).
CLOSING = "Both cards are already done and verified on the hub; no additional action is needed here."
HOUR = 3600.0


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, session_id TEXT,"
                     " block_kind TEXT, human_request TEXT)")
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [path])
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    yield path
    ksa._reset_cache_for_tests()


@pytest.fixture
def db(tmp_path, board):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="desktop")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def child(db, sid="child", *, started_ago, active_ago=None, ended=False, delegate_only=False):
    """An open delegate child row of ``chat``: started ``started_ago`` s ago, last active ``active_ago`` s ago."""
    if delegate_only:  # selected by model_config._delegate_from, not by source
        db.create_session(sid, source="desktop", parent_session_id="chat",
                          model_config={"_delegate_from": "chat"})
    else:
        db.create_session(sid, source="subagent", parent_session_id="chat")
    now = time.time()
    db._write_rowcount("UPDATE sessions SET title = ?, started_at = ?, last_activity_at = ?, ended_at = ? WHERE id = ?",
                       (f"Subagent: {sid}", now - started_ago,
                        None if active_ago is None else now - active_ago, now if ended else None, sid))


def reply(db, text=CLOSING, *, at=None):
    at = time.time() if at is None else at
    db.append_message("chat", role="user", content="what about those two cards?", timestamp=at)
    db.append_message("chat", role="assistant", content=text, timestamp=at + 1)


def declare(db, outcome, *, ago=0.0):
    """Declare ``outcome`` and backdate the stored declaration by ``ago`` seconds."""
    declared = so.declare_outcome("chat", outcome, db=db)
    if ago:
        declared = {**declared, "at": declared["at"] - ago}
        db._write_rowcount("UPDATE sessions SET outcome_json = ? WHERE id = 'chat'", (json.dumps(declared),))
    return declared


def test_stale_unterminated_subagent_with_a_closing_reply_is_lost_not_done(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    reply(db)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "subagent_lost")
    assert out["on"] == [{"kind": "subagent", "id": "child", "label": "Subagent: child", "state": "lost"}]


def test_a_delegate_child_selected_by_model_config_is_lost_too(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR, delegate_only=True)
    reply(db)
    assert so.derive_outcome("chat", db=db)["reason"] == "subagent_lost"


def test_no_activity_column_falls_back_to_started_at(db):
    child(db, started_ago=7 * HOUR)
    reply(db)
    assert so.derive_outcome("chat", db=db)["reason"] == "subagent_lost"


def test_a_fresh_subagent_still_waits(db):
    child(db, started_ago=2 * HOUR, active_ago=HOUR)
    reply(db)
    out = so.derive_outcome("chat", db=db)
    assert out["kind"] == "waiting"
    assert out["on"] == [{"kind": "subagent", "id": "child", "label": "Subagent: child", "state": "running"}]


def test_a_stale_subagent_older_than_the_bound_does_not_affect_the_outcome(db):
    child(db, started_ago=8 * 86400.0, active_ago=7.5 * 86400.0)
    reply(db)
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["summary"]) == ("done", CLOSING)


def test_an_ended_subagent_is_not_lost(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR, ended=True)
    reply(db)
    assert so.derive_outcome("chat", db=db)["kind"] == "done"


def test_a_declared_done_after_its_last_activity_accounts_for_it(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    reply(db, at=time.time() - 10)
    declared = declare(db, {"kind": "done", "summary": "Gate F verified; the stuck subagent was replaced."})
    out = so.refresh_outcome("chat", "turn_end", db=db)
    out.pop("status")  # [daveey] the canonical envelope rides beside what was declared (additive key)
    assert out == declared and out["kind"] == "done"


def test_the_accounting_survives_a_later_turn_that_drops_the_declaration(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    declare(db, {"kind": "done", "summary": "Gate F verified; the stuck subagent was replaced."}, ago=HOUR)
    so.refresh_outcome("chat", "turn_end", db=db)
    reply(db)  # a newer user row: the declaration no longer holds, the derived outcome replaces it
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert (out["kind"], out["source"], out["summary"]) == ("done", "derived", CLOSING)
    # ...and the next derivation still knows the subagent was accounted for.
    assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "done"


def test_a_declared_wait_after_its_last_activity_accounts_for_it(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    declare(db, {"kind": "waiting", "on": [{"kind": "time", "id": "tomorrow", "label": "re-run tomorrow"}]})
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


@pytest.mark.parametrize("outcome", [
    {"kind": "done", "summary": "Dispatched the Gate F verification."},
    {"kind": "waiting", "on": [{"kind": "subagent", "id": "child", "label": "Gate F verification"}]},
])
def test_a_declaration_older_than_its_last_activity_does_not_hide_it(db, outcome):
    """A done or a wait declared while the subagent still ran never waits forever on it, nor turns done."""
    child(db, started_ago=9 * HOUR, active_ago=7 * HOUR)
    declare(db, outcome, ago=8 * HOUR)
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "subagent_lost")


def test_live_work_still_waits_beside_a_lost_subagent(db, board):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO tasks VALUES ('t_run', 'nightly eval', 'running', 'chat', NULL, NULL)")
    ksa._reset_cache_for_tests()
    reply(db)
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_an_open_decision_outranks_a_lost_subagent(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    so.register_outcome_provider("steps", lambda sid: {"decision_items": [
        {"id": "s1", "question": "Ship it?", "choices": ["yes", "no"]}]})
    reply(db)
    assert so.derive_outcome("chat", db=db)["kind"] == "decision"


def test_an_unfinished_delegation_note_keeps_its_subagent_killed_reason(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    reply(db)
    db.append_message("chat", role="user", content="[ASYNC DELEGATION BATCH COMPLETE — d1] ...",
                      display_kind="async_delegation_complete",
                      display_metadata={"completion_note": ['["async_delegation","d1",""]'],
                                        "display_text": "Subagent Task Unknown: Finish post-deploy verification",
                                        "completion_admission": "a1"})
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "subagent_killed")


def test_a_lost_subagent_of_a_compressed_ancestor_counts(db):
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    db.create_session("tip", source="desktop", parent_session_id="chat")
    db._write_rowcount("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = 'chat'",
                       (time.time(),))
    db.append_message("tip", role="user", content="and now?")
    db.append_message("tip", role="assistant", content=CLOSING)
    assert so.derive_outcome("tip", db=db)["reason"] == "subagent_lost"


def test_compression_keeps_the_accounting_of_an_ancestor(db):
    """A declaration on the pre-compression row accounted for the lost child; the new tip must not forget it."""
    child(db, started_ago=8 * HOUR, active_ago=7 * HOUR)
    declare(db, {"kind": "done", "summary": "Gate F verified; the stuck subagent was replaced."}, ago=HOUR)
    so.refresh_outcome("chat", "turn_end", db=db)
    db.create_session("tip", source="desktop", parent_session_id="chat")
    db._write_rowcount("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = 'chat'",
                       (time.time(),))
    db.append_message("tip", role="user", content="and now?")
    db.append_message("tip", role="assistant", content=CLOSING)
    out = so.refresh_outcome("tip", "turn_end", db=db)
    assert (out["kind"], out["summary"]) == ("done", CLOSING)
    assert out["last_declared_at"] == pytest.approx(time.time() - HOUR, abs=60)
    assert so.refresh_outcome("tip", "turn_end", db=db)["kind"] == "done"
