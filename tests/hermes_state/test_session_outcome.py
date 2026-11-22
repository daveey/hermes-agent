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


@pytest.mark.parametrize("platform", ["kanban", "cli", "cron", "desktop"])
def test_2_durable_turn_without_marker_or_gateway_token_is_running(db, platform):
    import os

    from hermes_state_pidns import holder_namespace_token

    say(db, "user", "keep working")
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=chat:nonce:platform={platform}"
    assert db.try_acquire_session_turn_lease("chat", holder)
    try:
        assert so.refresh_outcome("chat", "manual", db=db)["kind"] == "running"
        assert so.get_outcome("chat", db=db)["kind"] == "running"
        assert so.derive_outcome("chat", db=db, ignore_live_turn=True)["reason"] == "interrupted"
        db.end_session("chat", "compression")
        db.create_session("tip", source=platform, parent_session_id="chat")
        db.append_message("tip", role="user", content="after compression")
        assert so.derive_outcome("tip", db=db)["kind"] == "running"
    finally:
        db.release_session_turn_lease("chat", holder)
    assert so.refresh_outcome("chat", "manual", db=db)["reason"] == "interrupted"


def test_2_expired_durable_turn_does_not_hide_interruption(db):
    import os

    from hermes_state_pidns import holder_namespace_token

    say(db, "user", "keep working")
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=chat:platform=kanban"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE session_turn_leases SET expires_at = ?", (time.time() - 1,))
    assert so.derive_outcome("chat", db=db)["reason"] == "interrupted"


def test_2_dead_durable_turn_owner_does_not_hide_interruption(db):
    import subprocess
    import sys

    from hermes_state_pidns import holder_namespace_token

    say(db, "user", "keep working")
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    holder = f"pid={child.pid}{holder_namespace_token()}:turn=chat:platform=kanban"
    try:
        assert db.try_acquire_session_turn_lease("chat", holder)
        assert so.derive_outcome("chat", db=db)["kind"] == "running"
        child.communicate(timeout=15)
        assert so.derive_outcome("chat", db=db)["reason"] == "interrupted"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)


def test_2_other_conversation_lease_does_not_hide_interruption(db):
    import os

    from hermes_state_pidns import holder_namespace_token

    say(db, "user", "keep working")
    db.create_session("other", source="kanban")
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=other:platform=kanban"
    assert db.try_acquire_session_turn_lease("other", holder)
    assert so.derive_outcome("chat", db=db)["reason"] == "interrupted"


def test_2_abandoned_durable_turn_is_not_running(db):
    import os

    from hermes_state_compression import _forget_abandoned_turn_lease, _mark_turn_lease_abandoned
    from hermes_state_pidns import holder_namespace_token

    say(db, "user", "keep working")
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=chat:platform=kanban"
    assert db.try_acquire_session_turn_lease("chat", holder)
    _mark_turn_lease_abandoned(db.db_path, holder)
    try:
        assert so.derive_outcome("chat", db=db)["reason"] == "interrupted"
    finally:
        _forget_abandoned_turn_lease(db.db_path, holder)


def test_2_audit_refresh_replaces_running_after_durable_turn_releases(db, monkeypatch):
    import os

    from hermes_cli import sessions_cmd_outcome as cli
    from hermes_state_pidns import holder_namespace_token

    # Real audit entry point and store; isolate only plugin discovery.
    monkeypatch.setattr(cli, "ensure_outcome_hooks", lambda: None)
    say(db, "user", "keep working")
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=chat:platform=kanban"
    assert db.try_acquire_session_turn_lease("chat", holder)
    assert cli.audit_rows(db, refresh=True)[0]["kind"] == "running"
    db.release_session_turn_lease("chat", holder)
    assert cli.audit_rows(db, refresh=True)[0]["reason"] == "interrupted"
    say(db, "assistant", SUMMARY)
    assert cli.audit_rows(db, refresh=True)[0]["kind"] == "done"
    assert so.get_outcome("chat", db=db)["kind"] == "done"


def _gateway_turn(db, tmp_path, *, started_at=None, token: str | None = "tok", sid="chat"):
    from datetime import UTC, datetime

    started = datetime.now(UTC) if started_at is None else started_at
    entry = {"session_id": sid, "active_turn_token": token, "active_turn_started_at": started.isoformat()}
    db.save_gateway_routing_entry(f"agent:main:local:dm:{sid}", json.dumps(entry),
                                  scope=str(tmp_path / "gwhome" / "sessions"))


def test_2_gateway_run_turn_is_running(db, tmp_path, monkeypatch):
    """A turn the messaging gateway runs (Desktop ``local`` platform too) writes no TUI marker, only the
    routing entry's active-turn token: mid-turn it ends on a user/tool row and must read running, not
    unresolved{interrupted}."""
    homes = []
    monkeypatch.setattr(so, "_gateway_running", lambda home: homes.append(home) or True)
    say(db, "user", "what's left?")
    assert so.derive_outcome("chat", db=db).get("reason") == "interrupted"
    _gateway_turn(db, tmp_path)
    assert so.refresh_outcome("chat", "manual", db=db)["kind"] == "running"
    assert homes[-1] == tmp_path / "gwhome"
    assert so.derive_outcome("chat", db=db, ignore_live_turn=True)["reason"] == "interrupted"


def test_2_gateway_turn_token_of_a_dead_gateway_is_not_running(db, tmp_path, monkeypatch):
    monkeypatch.setattr(so, "_gateway_running", lambda home: False)
    say(db, "user", "what's left?")
    _gateway_turn(db, tmp_path)
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out.get("reason")) == ("unresolved", "interrupted")


def test_2_gateway_turn_token_cleared_or_stale_or_other_chat_is_not_running(db, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(so, "_gateway_running", lambda home: True)
    say(db, "user", "what's left?")
    _gateway_turn(db, tmp_path, token=None)
    assert so.derive_outcome("chat", db=db)["kind"] == "unresolved"
    _gateway_turn(db, tmp_path, started_at=datetime.now(UTC) - timedelta(hours=7))
    assert so.derive_outcome("chat", db=db)["kind"] == "unresolved"
    _gateway_turn(db, tmp_path, sid="other")
    assert so.derive_outcome("chat", db=db)["kind"] == "unresolved"


def test_2_gateway_running_reads_that_homes_pid_record(tmp_path, monkeypatch):
    import gateway.status as gs

    seen = []
    monkeypatch.setattr(gs, "get_running_pid", lambda path, cleanup_stale=True: seen.append(
        (path, cleanup_stale)) or 4242)
    assert so._gateway_running(tmp_path) is True
    assert seen == [(tmp_path / "gateway.pid", False)]
    monkeypatch.setattr(gs, "get_running_pid", lambda path, cleanup_stale=True: None)
    assert so._gateway_running(tmp_path) is False


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
    out.pop("status")  # [daveey] the canonical envelope rides beside what was declared (additive key)
    assert out == declared and out["source"] == "declared"
    say(db, "user", "one more thing", timestamp=time.time() + 1)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out.pop("status")["reason"] == "interrupted"  # [daveey] additive envelope agrees
    assert out == {**so.derive_outcome("chat", db=db), "at": so.get_outcome("chat", db=db)["at"]}
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


@pytest.mark.parametrize("marker", ["[SILENT]", "NO_REPLY", " SILENT "])
def test_completed_silent_cron_is_done_without_a_judge(db, marker):
    db._write_rowcount("UPDATE sessions SET source='cron', end_reason='cron_complete' WHERE id='chat'", ())
    turn(db, marker)
    out = so.refresh_outcome("chat", "manual", db=db)
    assert out["kind"] == "done"
    assert out["summary"] == "Scheduled run completed with nothing to report."
    assert so.get_outcome("chat", db=db) == out


@pytest.mark.parametrize("source, end_reason", [
    ("desktop", "cron_complete"), ("cron", None), ("cron", "cron_incomplete_no_output"),
])
def test_silence_alone_does_not_prove_cron_completion(db, source, end_reason):
    db._write_rowcount("UPDATE sessions SET source=?, end_reason=? WHERE id='chat'", (source, end_reason))
    turn(db, "[SILENT]")
    assert so.refresh_outcome("chat", "manual", db=db)["reason"] == "no_summary"


@pytest.mark.parametrize("role, text, kw, expected", [
    ("assistant", "[SILENT]", {"finish_reason": "error"}, "turn_failed"),
    ("user", "card completed", {"display_kind": "kanban_notice"}, "notice_tail"),
    ("tool", "result", {"tool_name": "terminal", "tool_call_id": "c1"}, "interrupted"),
])
def test_completed_silent_cron_preserves_tail_errors(db, role, text, kw, expected):
    db._write_rowcount("UPDATE sessions SET source='cron', end_reason='cron_complete' WHERE id='chat'", ())
    turn(db, "[SILENT]")
    say(db, role, text, **kw)
    assert so.refresh_outcome("chat", "manual", db=db)["reason"] == expected


@pytest.mark.parametrize("status, human_request, expected", [
    ("running", None, "waiting"), ("blocked", HUMAN_REQUEST, "decision"),
])
def test_completed_silent_cron_preserves_live_work(db, board, status, human_request, expected):
    db._write_rowcount("UPDATE sessions SET source='cron', end_reason='cron_complete' WHERE id='chat'", ())
    turn(db, "[SILENT]")
    add_card(board, "t_work", "chat", status, human_request)
    assert so.refresh_outcome("chat", "manual", db=db)["kind"] == expected


def test_pending_silence_does_not_reuse_old_cron_completion(db):
    db._write_rowcount("UPDATE sessions SET source='cron', end_reason='cron_complete' WHERE id='chat'", ())
    turn(db, "[SILENT]")
    assert so.derive_outcome("chat", db=db, pending_reply="[SILENT]")["reason"] == "no_summary"


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


def test_a_notice_triggered_failed_turn_with_no_finish_reason_is_turn_failed_not_judged(db):
    # A kanban notice ran a turn that hit a provider 429: the closer typed its error row failed_turn with a NULL
    # finish_reason. _lost_request skips it (the trigger was a notice), so only the tail row can name it.
    so.register_outcome_judge(lambda sid, text, source=None: False)
    say(db, "assistant", SUMMARY)
    say(db, "user", "t_x done; PR1131 request_changes", display_kind=so._NOTICE_KIND)
    failed_turn(db, "Couldn't process the notice: HTTP 429 rate limit. Retrying once at 21:59 PT.")
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "turn_failed")
    assert "judged" not in out


def test_an_untyped_legacy_failed_turn_boundary_tail_is_turn_failed(db):
    from agent.turn_failure_copy import PARTIAL_FAILED_TURN_NOTICE
    so.register_outcome_judge(lambda sid, text, source=None: True)
    say(db, "user", "card moved", display_kind=so._NOTICE_KIND)
    say(db, "assistant", PARTIAL_FAILED_TURN_NOTICE)  # persisted before the closers typed it
    assert so.derive_outcome("chat", db=db)["reason"] == "turn_failed"


def test_a_reply_quoting_the_failed_turn_notice_is_still_judged(db):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    so.register_outcome_judge(lambda sid, text, source=None: True)
    say(db, "user", "what does the error row say?")
    say(db, "assistant", f"It says: {FAILED_TURN_NOTICE}")
    assert so.derive_outcome("chat", db=db)["kind"] == "done"


@pytest.mark.parametrize("typed", [True, False])
def test_an_interrupted_automation_boundary_tail_is_not_turn_failed(db, typed):
    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    so.register_outcome_judge(lambda sid, text, source=None: True)
    say(db, "user", "a cron delivery", display_kind="cron_delivery")
    if typed:
        failed_turn(db, AUTOMATION_INTERRUPTED_NOTICE)
    else:
        say(db, "assistant", AUTOMATION_INTERRUPTED_NOTICE)
    assert so.derive_outcome("chat", db=db)["kind"] == "done"


def test_a_real_reply_after_the_failure_clears_it(db, board):
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "archive my receipts")
    failed_turn(db)
    say(db, "user", "try again")
    say(db, "assistant", SUMMARY)
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_turn_end_clears_stored_turn_failed_after_a_later_good_reply(db, board):
    """Stored ``turn_failed`` (the auditor's row) must not survive a later good assistant
    reply once turn_end refresh runs — no manual ``--refresh``."""
    add_card(board, "t_run", "chat", "scheduled")
    say(db, "user", "archive my receipts")
    failed_turn(db)
    assert so.refresh_outcome("chat", "turn_end", db=db)["reason"] == "turn_failed"
    assert so.get_outcome("chat", db=db)["reason"] == "turn_failed"
    say(db, "assistant", SUMMARY)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "waiting"
    assert so.get_outcome("chat", db=db)["kind"] == "waiting"


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


# ── unviewed completion notes (#134889 LC-7) ───────────────────────────────


def note(db, display_text, kind="async_delegation_complete"):
    """What ``append_completion_note`` writes when a completion arrives with no viewer: a user row, no turn."""
    say(db, "user", "[ASYNC DELEGATION BATCH COMPLETE — d1] ...", display_kind=kind,
        display_metadata={"completion_note": ['["async_delegation","d1",""]'], "display_text": display_text,
                          "completion_admission": "a1"})


def test_a_finished_completion_note_leaves_the_chats_own_last_turn_as_its_word(db):
    turn(db)
    note(db, "Subagent Task Completed: implement the fix")
    note(db, "Finished: make", kind="process_complete")
    out = so.refresh_outcome("chat", "notice", db=db)
    assert (out["kind"], out["summary"]) == ("done", SUMMARY)


def test_an_unfinished_delegation_note_is_a_stopped_subagent_not_an_interrupted_turn(db):
    turn(db)
    note(db, "Subagent Task Interrupted: implement the fix")
    out = so.refresh_outcome("chat", "notice", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "subagent_killed")


def test_an_unfinished_note_under_a_finished_one_still_names_the_stopped_subagent(db):
    turn(db)
    note(db, "Subagent Tasks Finished with Issues: fan-out (2 tasks)")
    note(db, "Subagent Task Completed: other")
    out = so.refresh_outcome("chat", "notice", db=db)
    assert (out["kind"], out["reason"]) == ("unresolved", "subagent_killed")


def test_a_finished_note_does_not_break_a_declaration_or_hide_a_lost_request(db):
    say(db, "user", "do it")
    so.declare_outcome("chat", {"kind": "done", "summary": "Shipped."}, db=db)
    say(db, "assistant", "ok")
    note(db, "Subagent Task Completed: x")
    out = so.refresh_outcome("chat", "notice", db=db)
    assert (out["kind"], out["source"]) == ("done", "declared")
    say(db, "user", "and the docs?")
    note(db, "Subagent Task Completed: y")
    assert so.refresh_outcome("chat", "notice", db=db)["reason"] == "interrupted"


def test_a_typed_row_that_merely_mentions_completion_note_is_still_a_request(db):
    turn(db)
    say(db, "user", "what is a completion_note?")
    assert so.refresh_outcome("chat", "turn_end", db=db)["reason"] == "interrupted"


@pytest.mark.parametrize("text, unfinished", [
    ("Subagent Task Completed: a", False), ("Subagent Tasks Completed: a (2 tasks)", False),
    ("Subagent Task Interrupted: a", True), ("Subagent Task Failed: a", True), ("Subagent Task Incomplete: a", True),
    ("Subagent Tasks Finished with Issues: a (2 tasks)", True), ("Subagent Task Timed Out: a", True),
    ("Finished: make", False), ("", False),
])
def test_unfinished_reads_the_producers_own_titles(text, unfinished):
    from tools.process_registry_notifications import async_delegation_display_unfinished
    assert async_delegation_display_unfinished(text) is unfinished


@pytest.mark.parametrize("result, unfinished", [
    ({"status": "completed", "summary": "ok"}, False), ({"status": "interrupted"}, True),
    ({"status": "error", "error": "boom"}, True), ({"status": "timeout"}, True),
    ({"status": "completed", "summary": "ok", "exit_reason": "max_iterations"}, True),
])
def test_unfinished_agrees_with_the_title_producer(result, unfinished):
    from tools.process_registry_notifications import (async_delegation_display_text,
                                                      async_delegation_display_unfinished)
    for evt in ({"goal": "g", **result}, {"is_batch": True, "goals": ["g"], "results": [{"task_index": 0, **result}]}):
        assert async_delegation_display_unfinished(async_delegation_display_text(evt)) is unfinished

# ── failed card reads are unknown, not evidence of completion ────────────────

REAL_UNFINISHED = (
    "Resumed and cleared the placement blocker: refreshed the registry and moved the existing owner back.\n\n"
    "The saved evidence records the core change merged and independent safety review passed.\n\n"
    "**Remaining:** verify the loaded serving code and complete final acceptance review.\n\n"
    "The existing owner **t_01a1c7b8ebe8316f** is verified **ready at priority 9001**, not yet running.")


@pytest.mark.parametrize("verdict", [True, None])
@pytest.mark.parametrize("prior_kind", [None, "waiting", "decision", "done"])
def test_busy_card_read_cannot_derive_done(db, board, monkeypatch, verdict, prior_kind):
    from hermes_cli.kanban_generation import BoardLifecycleBusy

    add_card(board, "t_open", "chat", "ready")
    turn(db)
    prior = {"kind": prior_kind, "source": "derived", "at": 1}
    if prior_kind:
        so._write(db, "chat", prior)

    def busy(*args, **kwargs):
        raise BoardLifecycleBusy("database is locked: board lifecycle is busy")

    monkeypatch.setattr(ksa, "session_outcome_tasks", busy)
    so.register_outcome_judge(lambda sid, text: verdict)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    if prior_kind in ("waiting", "decision"):
        assert out.pop("status")["coverage"]["state"] == "unknown"  # [daveey] additive envelope: unread = unknown
        assert out == prior
    else:
        assert (out["kind"], out["reason"]) == ("unresolved", "card_read_failed")


@pytest.mark.parametrize("status", ["triage", "running", "blocked"])
@pytest.mark.parametrize("declared", [False, True])
def test_empty_card_list_cannot_override_open_counts(db, board, monkeypatch, status, declared):
    turn(db)
    if declared:
        so.declare_outcome("chat", {"kind": "done", "summary": SUMMARY}, db=db)
    add_card(board, "t_01a1d0b341560679", "chat", status,
             HUMAN_REQUEST if status == "blocked" else None)
    # Counts retain an open obligation while the independently cached list misses it.
    assert ksa.active_task_counts().get("chat", 0) + ksa.blocked_task_counts().get("chat", 0) == 1
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **kw: [])
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out.get("reason")) == ("unresolved", "card_read_failed")


def test_empty_card_list_checks_compression_ancestor_counts(db, board, monkeypatch):
    turn(db)
    db.end_session("chat", "compression")
    db.create_session("tip", source="desktop", parent_session_id="chat")
    db.append_message("tip", role="assistant", content=SUMMARY)
    add_card(board, "t_01a1d0b341560679", "chat", "triage")
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **kw: [])
    out = so.derive_outcome("tip", db=db)
    assert (out["kind"], out.get("reason")) == ("unresolved", "card_read_failed")


def test_stale_empty_remote_projection_is_not_completion(db, monkeypatch):
    turn(db)
    scope = ksa._origin_scope()
    card = {"id": "t_01a1d0b341560679", "key": "worker-4/default/default/t_01a1d0b341560679",
            "gateway": "worker-4", "profile": "default", "board": "default", "session": "chat",
            "status": "triage", "title": "remote work", "unfinished": True}
    monkeypatch.setattr(ksa, "_links_cache", {scope: (0, scope, ("old",), [], [])})
    monkeypatch.setattr(ksa, "_links_fingerprint", lambda: ("new",))
    monkeypatch.setattr(ksa, "_project_remote_links", lambda: [card])
    monkeypatch.setattr(ksa, "_refresh_links_in_background", lambda scope: None)
    monkeypatch.setattr(ksa, "_remote_cards", lambda ids, statuses, stale_ok: ksa._remote_links(stale_ok))
    # Exercise the real stale-list/strict-count paths, not a mocked empty task API.
    assert ksa.session_tasks(["chat"], stale_ok=True) == []
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out.get("reason")) == ("unresolved", "card_read_failed")
    assert ksa.active_task_counts()["chat"] == 1
    assert so.derive_outcome("chat", db=db)["kind"] == "waiting"


def test_empty_counts_failure_is_not_completion(db, monkeypatch):
    turn(db)

    def unreadable():
        raise sqlite3.OperationalError("counts unavailable")

    monkeypatch.setattr(ksa, "active_task_counts", unreadable)
    out = so.derive_outcome("chat", db=db)
    assert (out["kind"], out.get("reason")) == ("unresolved", "card_read_failed")


def test_same_unfinished_reply_with_the_card_counted_is_waiting(db, board):
    add_card(board, "t_01a1c7b8ebe8316f", "chat", "ready")
    so.register_outcome_judge(lambda sid, text: True)
    turn(db, REAL_UNFINISHED)
    out = so.refresh_outcome("chat", "turn_end", db=db)
    assert out["kind"] == "waiting" and out["on"][0]["id"] == "t_01a1c7b8ebe8316f"


@pytest.mark.parametrize("reply", [
    SUMMARY,  # no card named
    "Completed t_01a1c7b8ebe8316f; all tests pass. No remaining work.",
    "Earlier t_01a1c7b8ebe8316f was not yet running; it has now completed and all tests pass.",
    "Shipped the fix in t_01a1c7b8ebe8316f, which merged; all 12 tests pass and nothing else is open.",  # card done
    "Nothing else to do. The explanation: stale cache, cleared, verified by running the suite twice.",
])
def test_truly_done_replies_stay_done(db, reply):
    so.register_outcome_judge(lambda sid, text: True)
    turn(db, reply)
    assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "done"


@pytest.mark.parametrize("failed_outer", [True, False])
def test_same_sid_interleaved_card_reads_are_invocation_local(db, monkeypatch, failed_outer):
    from hermes_cli.kanban_generation import BoardLifecycleBusy

    turn(db, "Fixed and merged via t_01a1c7b8ebe8316f; all 12 tests pass.")
    so.register_outcome_judge(lambda sid, text: True)
    nested = []
    reading_outer = True

    def cards(*args, **kwargs):
        if reading_outer == failed_outer:
            raise BoardLifecycleBusy("database is locked: board lifecycle is busy")
        return []

    def provider(sid):
        nonlocal reading_outer
        if reading_outer:
            reading_outer = False
            nested.append(so.derive_outcome(sid, db=db))
        return {}

    monkeypatch.setattr(ksa, "session_outcome_tasks", cards)
    so.register_outcome_provider("interleave", provider)
    outer = so.derive_outcome("chat", db=db)
    failed, succeeded = (outer, nested[0]) if failed_outer else (nested[0], outer)
    assert succeeded["kind"] == "done"
    assert (failed["kind"], failed["reason"]) == ("unresolved", "card_read_failed")


def test_human_decision_card_still_outranks_the_reply(db, board):
    add_card(board, "t_01a1c7b8ebe8316f", "chat", "blocked", HUMAN_REQUEST)
    so.register_outcome_judge(lambda sid, text: True)
    turn(db, REAL_UNFINISHED)
    assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "decision"


@pytest.mark.parametrize("precedence", ["declared", "declared_waiting", "provider", "silent_cron"])
def test_busy_card_read_preserves_authority_but_not_silent_completion(db, monkeypatch, precedence):
    from hermes_cli.kanban_generation import BoardLifecycleBusy

    turn(db)
    declared = None
    if precedence == "declared":
        declared = so.declare_outcome("chat", {"kind": "done", "summary": SUMMARY}, db=db)
    elif precedence == "declared_waiting":
        declared = so.declare_outcome("chat", {"kind": "waiting", "on": [{"kind": "card", "id": "t_x"}]}, db=db)
    elif precedence == "provider":
        so.register_outcome_provider("approval", lambda sid: {
            "decision_items": [{"id": "approval", "question": "Deploy?"}]})
    else:
        db._write_rowcount("UPDATE sessions SET source='cron', end_reason='cron_complete' WHERE id='chat'", ())
        say(db, "assistant", "[SILENT]")

    def busy(*args, **kwargs):
        raise BoardLifecycleBusy("database is locked: board lifecycle is busy")

    monkeypatch.setattr(ksa, "session_outcome_tasks", busy)
    out = so.refresh_outcome("chat", "manual", db=db)
    if precedence == "declared":
        # A declared done cannot outrank an unread card any more than a counted open one.
        assert out["kind"] != "done"
        assert (out["kind"], out["reason"]) == ("unresolved", "card_read_failed")
        assert out["declared"]["kind"] == "done"
    elif precedence == "declared_waiting":
        assert out["kind"] == "waiting" and out["on"] == (declared or {})["on"]
    elif precedence == "provider":
        assert out["kind"] == "decision" and out["items"][0]["id"] == "approval"
    else:
        assert (out["kind"], out["reason"]) == ("unresolved", "card_read_failed")
