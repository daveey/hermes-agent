"""Stored chat outcome (hermes_state_outcome): declare / derive / refresh, one test per precedence
branch of the session-outcome contract, plus the judge and provider hooks and list rows."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tui_gateway.turn_marker import clear_turn_marker, record_turn_start

HUMAN_REQUEST = {
    "action": "Approve the production deploy?", "why_human": "Only David can approve a deploy.",
    "interaction": "approval", "scope": "deploy", "evidence": "CI is green.",
    "choices": [{"label": "Ship it", "value": "yes"}, {"label": "Hold", "value": "no"}],
}
SUMMARY = "Fixed the flaky test by waiting on the event instead of sleeping; all 12 tests pass now."


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


def add_card(path, tid, sid, status, request=None, title="a card"):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                     (tid, title, status, sid, "needs_input" if request else None,
                      json.dumps(request) if request else None))
    ksa._reset_cache_for_tests()


@pytest.fixture
def db(tmp_path, board):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="desktop")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def say(db, role, content, **kw):
    db.append_message("chat", role=role, content=content, **kw)


def turn(db, reply=SUMMARY):
    say(db, "user", "please fix the flaky test")
    say(db, "assistant", reply)


# ── precedence branches 1-8 ────────────────────────────────────────────────


def test_1_muted_wins_over_everything(db, board):
    add_card(board, "t_ask", "chat", "blocked", HUMAN_REQUEST)
    turn(db)
    out = so.set_muted("chat", True, db=db)
    assert out["kind"] == "muted"
    assert so.get_outcome("chat", db=db)["kind"] == "muted"
    assert so.set_muted("chat", False, db=db)["kind"] == "decision"


def test_2_live_turn_is_running(db):
    turn(db)
    home = db.db_path.parent
    record_turn_start(home, "chat", "keep going")
    try:
        assert so.derive_outcome("chat", db=db)["kind"] == "running"
        assert so.derive_outcome("chat", db=db, ignore_live_turn=True)["kind"] == "done"
    finally:
        clear_turn_marker(home, "chat")
    assert so.derive_outcome("chat", db=db)["kind"] == "done"


def test_3_blocked_human_card_is_a_decision_with_its_choices(db, board):
    add_card(board, "t_ask", "chat", "blocked", HUMAN_REQUEST)
    add_card(board, "t_run", "chat", "running")
    turn(db)
    out = so.refresh_outcome("chat", "card_change", db=db)
    assert out["kind"] == "decision"
    assert out["items"] == [{"id": "card:t_ask", "question": HUMAN_REQUEST["action"],
                             "choices": HUMAN_REQUEST["choices"], "card_id": "t_ask", "state": "open"}]


def test_4_live_work_is_waiting_with_each_item_named(db, board):
    add_card(board, "t_run", "chat", "scheduled", title="nightly eval")
    db.create_session("child", source="subagent", parent_session_id="chat")
    turn(db, "I'll check back when the eval lands.")
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "waiting"
    assert {(w["kind"], w["id"]) for w in out["on"]} == {("card", "t_run"), ("subagent", "child")}
    assert next(w for w in out["on"] if w["id"] == "t_run")["label"] == "nightly eval"


def test_5_declared_done_holds_until_a_newer_user_row(db):
    say(db, "user", "do it")
    declared = so.declare_outcome("chat", {"kind": "done", "summary": "Shipped."}, db=db)
    say(db, "tool", "ok", tool_name="session_outcome", tool_call_id="c1")
    say(db, "assistant", "ok?")  # would be no_summary without the declaration
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out == declared and out["source"] == "declared"
    say(db, "user", "one more thing", timestamp=time.time() + 1)
    assert so.refresh_outcome("chat", "turn_end", db=db) == {**so.derive_outcome("chat", db=db),
                                                              "at": so.get_outcome("chat", db=db)["at"]}
    assert so.get_outcome("chat", db=db)["reason"] == "interrupted"


def test_5_declaration_survives_a_wait_that_ends(db, board):
    say(db, "user", "do it")
    so.declare_outcome("chat", {"kind": "done", "summary": "Shipped."}, db=db)
    say(db, "assistant", "ok?")
    add_card(board, "t_run", "chat", "running")
    assert so.refresh_outcome("chat", "card_change", db=db)["kind"] == "waiting"
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET status = 'done'")
    ksa._reset_cache_for_tests()
    out = so.refresh_outcome("chat", "card_change", db=db)
    assert (out["kind"], out["source"], out["summary"]) == ("done", "declared", "Shipped.")


@pytest.mark.parametrize("rows, reason", [
    ([("assistant", SUMMARY, {}), ("user", "card t_1 done", {"display_kind": "kanban_notice"})], "notice_tail"),
    ([("user", "hello", {})], "interrupted"),
    ([("user", "go", {}), ("assistant", "", {"tool_calls": [{"id": "c", "function": {"name": "x"}}]}),
      ("tool", "result", {"tool_call_id": "c", "tool_name": "terminal"})], "interrupted"),
    ([("user", "go", {}), ("assistant", "partial answer that broke", {"finish_reason": "error"})], "turn_failed"),
])
def test_6_tail_shape_is_unresolved_with_its_reason(db, rows, reason):
    for role, content, kw in rows:
        say(db, role, content, **kw)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", reason)


def test_7_summary_is_done_otherwise_no_summary(db):
    turn(db, SUMMARY + " " + "x" * 600)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "done" and len(out["summary"]) == so.SUMMARY_CHARS
    say(db, "user", "and?")
    say(db, "assistant", "Should I also bump the version?")
    assert so.refresh_outcome("chat", "turn_end", db=db)["reason"] == "no_summary"


def test_7_pending_reply_is_judged_instead_of_the_saved_tail(db):
    turn(db, "Let me look at the logs next.")
    assert so.derive_outcome("chat", db=db)["reason"] == "no_summary"
    out = so.derive_outcome("chat", db=db, pending_reply=SUMMARY)
    assert out["kind"] == "done" and out["summary"] == SUMMARY
    assert so.get_outcome("chat", db=db) is None, "derive never writes"


def test_8_no_messages_is_empty(db):
    assert so.refresh_outcome("chat", "manual", db=db) == {**so.get_outcome("chat", db=db)}
    assert so.get_outcome("chat", db=db)["reason"] == "empty"


# ── hooks, writes, validation ──────────────────────────────────────────────


@pytest.mark.parametrize("verdict, kind", [(True, "done"), (False, "unresolved"), (None, "done")])
def test_judge_true_false_and_none_falls_back_to_the_heuristic(db, verdict, kind):
    seen = []
    so.register_outcome_judge(lambda sid, text: seen.append((sid, text)) or verdict)
    turn(db)
    assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == kind
    assert seen == [("chat", SUMMARY)]


def test_judge_true_marks_a_short_reply_done(db):
    so.register_outcome_judge(lambda sid, text: True)
    turn(db, "Done.")
    assert so.refresh_outcome("chat", "turn_end", db=db)["summary"] == "Done."


def test_provider_supplies_decisions_and_waits(db):
    so.register_outcome_provider("steps", lambda sid: {"decision_items": [
        {"id": "us_1", "question": "Merge it?", "choices": [{"label": "Yes", "value": "y"}, {"label": "No"}]},
        {"id": "us_2", "question": "Old one", "state": "answered"}]})
    turn(db)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "decision"
    assert out["items"] == [{"id": "us_1", "question": "Merge it?", "state": "open", "choices": [
        {"label": "Yes", "value": "y"}, {"label": "No", "value": "No"}]}]
    so.register_outcome_provider("steps", lambda sid: {"waiting_on": [
        {"kind": "capacity", "id": "hub", "label": "hub slot", "eta": 1793300000}]})
    so.register_outcome_provider("broken", lambda sid: 1 / 0)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["on"] == [{"kind": "capacity", "id": "hub", "label": "hub slot", "eta": 1793300000.0}]


def test_refresh_writes_only_when_the_outcome_changed(db):
    turn(db)
    first = so.refresh_outcome("chat", "turn_end", db=db)
    stamped = db.get_session("chat")["outcome_at"]
    assert so.refresh_outcome("chat", "manual", db=db) == first
    assert db.get_session("chat")["outcome_at"] == stamped


@pytest.mark.parametrize("bad", [
    {"kind": "done"}, {"kind": "running"}, {"kind": "decision", "items": [{"question": "q", "choices": ["a"]}]},
    {"kind": "waiting", "on": [{"kind": "vibes", "id": "x"}]}, "done",
])
def test_declare_rejects_invalid_outcomes(db, bad):
    with pytest.raises(ValueError):
        so.declare_outcome("chat", bad, db=db)
    with pytest.raises(ValueError):
        so.refresh_outcome("chat", "whenever", db=db)


def test_declared_waiting_and_decision_are_stored(db):
    out = so.declare_outcome("chat", {"kind": "waiting", "on": [{"kind": "time", "id": "14:00"}]}, db=db)
    assert out["on"] == [{"kind": "time", "id": "14:00", "label": "14:00"}]
    out = so.declare_outcome("chat", {"kind": "decision", "items": [
        {"id": "us_9", "question": "Which?", "choices": ["a", "b"], "card_id": "t_1"}]}, db=db)
    assert so.get_outcome("chat", db=db) == out and out["items"][0]["card_id"] == "t_1"


def test_list_sessions_rich_rows_carry_outcome_and_muted(db):
    turn(db)
    so.refresh_outcome("chat", "turn_end", db=db)
    rows = db.list_sessions_rich(order_by_last_active=True, include_pinned=True, compact_rows=True)
    row = next(r for r in rows if r["id"] == "chat")
    assert row["outcome"]["kind"] == "done" and row["muted"] is False
    so.set_muted("chat", True, db=db)
    row = next(r for r in db.list_sessions_rich(limit=5) if r["id"] == "chat")
    assert row["outcome"]["kind"] == "muted" and row["muted"] is True


# ── A lost request and a recurring cron do not hide behind live work ───────

def failed_turn(db, text=None, **kw):
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE
    say(db, "assistant", text or FAILED_TURN_NOTICE, display_kind=FAILED_TURN_DISPLAY_KIND, **kw)


def test_failed_typed_request_outranks_an_open_card(db, board):
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "archive my receipts")
    failed_turn(db)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "turn_failed")


def test_failed_request_outranks_a_cron_and_a_later_refused_automation_row(db, board, monkeypatch):
    monkeypatch.setattr(so, "_cron_items", lambda ids: [{"kind": "cron", "id": "j", "label": "sweep"}])
    say(db, "user", "and mercury notices")
    failed_turn(db)
    say(db, "user", "[IMPORTANT: Background process done]", display_kind="process_complete")
    failed_turn(db)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "turn_failed")


def test_failed_turn_with_no_live_work_is_turn_failed_not_judged(db):
    so.register_outcome_judge(lambda sid, text, source=None: True)
    say(db, "user", "archive my receipts")
    failed_turn(db)
    assert so.derive_outcome("chat", db=db)["reason"] == "turn_failed"


def test_a_real_reply_after_the_failure_clears_it(db, board):
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "archive my receipts")
    failed_turn(db)
    say(db, "user", "try again")
    say(db, "assistant", SUMMARY)
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_interrupted_automation_delivery_does_not_paint_a_waiting_chat(db, board):
    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "a cron delivery with no display kind")
    failed_turn(db, AUTOMATION_INTERRUPTED_NOTICE)
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_a_typed_row_nobody_answered_outranks_an_open_card(db, board):
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "are you there")
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "interrupted")


def test_a_kanban_notice_tail_stays_a_wait_when_the_card_is_open(db, board):
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "assistant", SUMMARY)
    say(db, "user", "card moved", display_kind=so._NOTICE_KIND)
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_an_open_decision_still_outranks_a_failed_request(db, board):
    add_card(board, "t_ask", "chat", "blocked", request=HUMAN_REQUEST)
    say(db, "user", "archive my receipts")
    failed_turn(db)
    assert so.derive_outcome("chat", db=db)["kind"] == "decision"


def test_a_recurring_cron_is_not_a_wait_a_finished_chat_is_done_and_names_it(db, monkeypatch):
    monkeypatch.setattr(so, "_cron_items", lambda ids: [{"kind": "cron", "id": "j1", "label": "sweep"}])
    so.register_outcome_judge(lambda sid, text, source=None: True)
    turn(db, SUMMARY)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "done"
    assert [(c["id"], c["label"]) for c in out["crons"]] == [("j1", "sweep")]


def test_a_declared_done_keeps_naming_its_cron(db, monkeypatch):
    monkeypatch.setattr(so, "_cron_items", lambda ids: [{"kind": "cron", "id": "j1", "label": "sweep"}])
    say(db, "user", "do it")
    so.declare_outcome("chat", {"kind": "done", "summary": "Shipped."}, db=db)
    say(db, "tool", "ok", tool_name="session_outcome", tool_call_id="c1")
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["source"]) == ("done", "declared")
    assert out["crons"][0]["id"] == "j1"


def test_an_open_card_still_waits_beside_a_cron(db, board, monkeypatch):
    monkeypatch.setattr(so, "_cron_items", lambda ids: [{"kind": "cron", "id": "j1", "label": "sweep"}])
    add_card(board, "t_run", "chat", "scheduled")
    turn(db, SUMMARY)
    out = so.derive_outcome("chat", db=db)
    assert out["kind"] == "waiting" and [w["id"] for w in out["on"]] == ["t_run"]


def test_a_declared_wait_on_a_cron_is_still_honoured(db):
    say(db, "user", "do it")
    so.declare_outcome("chat", {"kind": "waiting", "on": [{"kind": "cron", "id": "j1"}]}, db=db)
    say(db, "tool", "ok", tool_name="session_outcome", tool_call_id="c1")
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"
