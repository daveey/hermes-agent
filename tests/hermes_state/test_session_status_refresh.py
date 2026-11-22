"""Canonical status through the real ``refresh_outcome`` entry point: existing readers, strict process
inventory, tokened turn settlement, notice transparency and the fenced publication. [daveey]"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tools import process_registry as registry_module
from tools import process_registry_results as results
from tools.process_registry import ProcessSession
from tui_gateway.turn_marker import clear_turn_marker, read_turn_marker, record_turn_start

HUMAN_REQUEST = {
    "action": "Approve the production deploy?", "why_human": "Only David can approve a deploy.",
    "interaction": "approval", "scope": "deploy", "evidence": "CI is green.",
    "choices": [{"label": "Ship it", "value": "yes"}, {"label": "Hold", "value": "no"}],
}


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
def receipts(tmp_path, monkeypatch):
    """The real retained-result directory and registry, scoped to this test's home."""
    monkeypatch.setattr(results, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(results, "_owns_result", lambda owner, parent: owner == parent)
    registry = registry_module.process_registry
    monkeypatch.setattr(registry, "_running", {})
    monkeypatch.setattr(registry, "_finished", {})
    directory = tmp_path / "logs" / "process-results"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def db(tmp_path, board, receipts):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="desktop")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def add_card(path, tid, status, request=None):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                     (tid, "a card", status, "chat", "needs_input" if request else None,
                      json.dumps(request) if request else None))
    ksa._reset_cache_for_tests()


def set_card(path, tid, status):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET status = ?, block_kind = NULL, human_request = NULL WHERE id = ?", (status, tid))
    ksa._reset_cache_for_tests()


def say(db, role, content, **kw):
    db.append_message("chat", role=role, content=content, **kw)


def turn(db, reply="ok"):
    say(db, "user", "please fix the flaky test")
    say(db, "assistant", reply)


def receipt(directory, exit_code=0, pid="proc_example", at=None):
    data = dict(id=pid, command="true", cwd="/", task_id="task", owner_task_id="task", session_key="",
                parent_session_id="chat", started_at=time.time(), exit_code=exit_code,
                completion_reason="exited", termination_source="", notify_on_complete=False, output="")
    path = directory / f"{pid}.json"
    path.write_text(json.dumps(data))
    if at is not None:
        import os
        os.utime(path, (at, at))
    return path


def refresh(db, trigger="turn_end", **kw):
    return so.refresh_outcome("chat", trigger, db=db, **kw)["status"]


def assert_done(status):
    assert (status["kind"], status["presentation"]["stripe"], status["coverage"]["state"]) == ("done", "done", "complete")


def test_declaration_cron_receipt_cannot_restore_an_older_status_revision(db, board, monkeypatch):
    import cron.session_ownership as ownership
    turn(db)
    first = refresh(db)
    successor = []

    def release(*args, **kwargs):
        add_card(board, "t_running", "running")
        successor.append(refresh(db, "card_change"))
        return [], []

    monkeypatch.setattr(ownership, "release_for_database", release)
    so.declare_outcome("chat", {"kind": "done", "summary": "all done"}, db=db)
    assert successor[0]["revision"] > first["revision"]
    assert so.get_outcome("chat", db=db)["status"] == successor[0]


@pytest.mark.parametrize("value", [[], {"decision_items": [{}]}, {"waiting_on": [{"kind": "unsupported", "id": "work"}]}])
def test_malformed_control_source_is_unknown_then_recovers(db, value):
    turn(db)
    so.register_outcome_provider("invalid", lambda sid: value)
    status = refresh(db)
    assert status["coverage"]["state"] == "unknown"
    assert status["presentation"]["stripe"] != "done"
    so.register_outcome_provider("invalid", lambda sid: None)
    assert_done(refresh(db))


@pytest.mark.parametrize("value", [{"status": "active"}, {"status": "unsupported", "goal": "work"}])
def test_malformed_goal_source_is_unknown_then_recovers(db, value):
    turn(db)
    db._write_rowcount("INSERT INTO state_meta(key, value) VALUES (?, ?)", ("goal:chat", json.dumps(value)))
    status = refresh(db)
    assert status["coverage"]["state"] == "unknown"
    assert status["presentation"]["stripe"] != "done"
    db._write_rowcount("DELETE FROM state_meta WHERE key = ?", ("goal:chat",))
    assert_done(refresh(db))


def test_transcript_read_failure_publishes_unknown_instead_of_leaving_stored_green(db, monkeypatch):
    turn(db)
    first = refresh(db)
    tail = so._tail

    def fail(*args):
        raise sqlite3.OperationalError("transcript temporarily unavailable")

    monkeypatch.setattr(so, "_tail", fail)
    failed = refresh(db)
    assert failed["coverage"]["state"] == "unknown"
    assert failed["presentation"]["stripe"] != "done"
    assert failed["revision"] > first["revision"]
    assert so.get_outcome("chat", db=db)["status"] == failed
    monkeypatch.setattr(so, "_tail", tail)
    assert_done(refresh(db))


def test_observed_child_lost_end_without_parent_notice_never_becomes_green(db):
    turn(db)
    db.create_session("child", source="subagent", parent_session_id="chat")
    assert refresh(db)["kind"] == "waiting"  # linked child work, not the parent's own running turn
    db.end_session("child", end_reason="delegation_unknown")
    lost = refresh(db)
    assert lost["presentation"]["stripe"] != "done"
    assert any(item["id"] == "subagent:child" for item in lost["failures"])


@pytest.mark.parametrize("reader", ["_generation", "_identity"])
def test_turn_metadata_failure_publishes_unknown_then_recovers(db, monkeypatch, reader):
    import hermes_state_session_status_snapshot as snapshot
    turn(db)
    first = refresh(db)
    original = getattr(snapshot, reader)

    def fail(*args):
        raise sqlite3.OperationalError("turn metadata read failed")

    monkeypatch.setattr(snapshot, reader, fail)
    failed = refresh(db)
    assert failed["coverage"]["state"] == "unknown"
    assert failed["presentation"]["stripe"] != "done"
    assert failed["revision"] > first["revision"]
    assert so.get_outcome("chat", db=db)["status"] == failed
    monkeypatch.setattr(snapshot, reader, original)
    assert_done(refresh(db))


# ── row 1: all work terminal and the owning turn settled is green, whatever the reply says ────────


@pytest.mark.parametrize("reply", ["ok", "Let me look into that next?", "Fixed it; all 12 tests pass now."])
def test_all_terminal_work_is_done_complete_regardless_of_wording(db, board, receipts, reply):
    add_card(board, "t_done", "done")
    receipt(receipts, 0)
    turn(db, reply)
    status = refresh(db)
    assert_done(status)
    assert status["status_version"] == 1 and status["revision"] == 1
    assert status["identity"]["session_id"] == "chat"
    assert set(status["source_revisions"]) == {"cards", "controls", "turn", "subagents", "processes", "goals"}


def test_repeated_refresh_and_reads_never_change_revision_or_paint(db):
    turn(db)
    first = refresh(db)
    for trigger in ("manual", "notice", "card_change", "reap", "manual"):
        assert refresh(db, trigger) == first
    assert so.get_outcome("chat", db=db)["status"] == first
    rows = so.stamp_outcome_rows([dict(db._read_one("SELECT id, outcome_json, muted FROM sessions WHERE id='chat'"))])
    assert rows[0]["outcome"]["status"] == first


def test_declaration_keeps_the_envelope_and_has_no_status_authority(db, board):
    add_card(board, "t_open", "running")
    turn(db)
    assert refresh(db)["kind"] == "waiting"
    declared = so.declare_outcome("chat", {"kind": "done", "summary": "all finished"}, db=db)
    assert declared["kind"] == "done"
    stored = so.get_outcome("chat", db=db)
    assert stored["status"]["kind"] == "waiting" and stored["status"]["revision"] == 1
    status = refresh(db, "manual")
    assert (status["kind"], status["revision"]) == ("waiting", 1)
    set_card(board, "t_open", "done")
    status = refresh(db, "card_change")
    assert_done(status)
    assert status["revision"] == 2


# ── notices are transparent; a genuine request starts a generation ─────────────────────────────


def test_informational_notice_does_not_reopen_a_done_session(db):
    turn(db)
    done = refresh(db)
    assert_done(done)
    say(db, "user", "[kanban] card t_x finished", display_kind="kanban_notice",
        display_metadata={"outcome_informational": True})
    assert refresh(db, "notice") == done
    say(db, "user", "[ASYNC DELEGATION BATCH COMPLETE — d1] ...", display_kind="async_delegation_complete",
        display_metadata={"completion_note": ['["async_delegation","d1",""]'], "display_text": "finished"})
    assert refresh(db, "notice") == done


def test_genuine_or_unproven_user_text_starts_a_new_generation(db):
    turn(db)
    done = refresh(db)
    say(db, "user", "[kanban] card t_x finished\n\nalso please rerun the suite", display_kind="kanban_notice",
        display_metadata={})  # folded/unproven: not a provably informational notice
    status = refresh(db, "notice")
    assert status["generation"] > done["generation"]
    assert status["presentation"]["stripe"] == "attention" and status["kind"] == "unresolved"
    say(db, "user", "one more thing")
    newer = refresh(db, "manual")
    assert newer["generation"] > status["generation"] and newer["kind"] == "unresolved"
    say(db, "assistant", "done")
    assert_done(refresh(db))


# ── row 4: a failed authoritative read is unknown (never the old green); recovery is green ─────────


def test_unreadable_process_inventory_is_unknown_then_recovers(db, receipts):
    turn(db)
    done = refresh(db)
    assert_done(done)
    bad = receipts / "proc_bad.json"
    bad.write_text("{")
    unknown = refresh(db, "manual")
    assert (unknown["kind"], unknown["reason"], unknown["presentation"]["stripe"]) == (
        "unresolved", "source_unknown", "attention")
    assert unknown["coverage"]["state"] == "unknown"
    assert any(reason.startswith("processes:process_read_failed") for reason in unknown["coverage"]["reasons"])
    assert unknown["revision"] == done["revision"] + 1
    assert so.get_outcome("chat", db=db)["status"] == unknown  # published, not only returned
    bad.unlink()
    recovered = refresh(db, "manual")
    assert_done(recovered)
    assert recovered["revision"] == unknown["revision"] + 1


def test_inventory_enumeration_failure_is_unknown(db, receipts, monkeypatch):
    turn(db)
    assert_done(refresh(db))

    def fail(self):
        raise PermissionError("cannot enumerate")
    monkeypatch.setattr(type(receipts), "iterdir", fail)
    assert refresh(db, "manual")["reason"] == "source_unknown"


@pytest.mark.parametrize("source,patch", [("cards", "_card_items"), ("subagents", "_subagent_items")])
def test_a_failed_reader_is_unknown_not_empty(db, monkeypatch, source, patch):
    turn(db)
    assert_done(refresh(db))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("unreadable")
    monkeypatch.setattr(so, patch, fail)
    status = refresh(db, "manual")
    assert status["reason"] == "source_unknown"
    assert any(reason.startswith(f"{source}:") for reason in status["coverage"]["reasons"])


def test_a_raising_control_provider_is_unknown(db):
    turn(db)

    def provider(sid):
        raise RuntimeError("vault unreadable")
    so.register_outcome_provider("vault", provider)
    status = refresh(db)
    assert status["reason"] == "source_unknown"
    assert any(reason.startswith("controls:") for reason in status["coverage"]["reasons"])


def test_malformed_goal_record_is_unknown(db):
    turn(db)
    db._write_rowcount("INSERT INTO state_meta (key, value) VALUES ('goal:chat', '{')")
    assert refresh(db)["reason"] == "source_unknown"


# ── row 6: open, failed, lost or unreadable work can never be green ──────────────────────────────


def test_open_card_waits_and_human_ask_is_a_decision(db, board):
    add_card(board, "t_open", "blocked")
    turn(db)
    assert (refresh(db)["kind"], refresh(db)["presentation"]["stripe"]) == ("waiting", "waiting")
    add_card(board, "t_ask", "blocked", HUMAN_REQUEST)
    status = refresh(db, "card_change")
    assert (status["kind"], status["presentation"]["stripe"]) == ("decision", "stuck")


def test_live_process_waits_failed_exit_is_attention_until_a_successful_successor(db, receipts):
    live = ProcessSession(id="proc_live", command="sleep 60", cwd="/", task_id="task", parent_session_id="chat")
    registry = registry_module.process_registry
    registry._running[live.id] = live
    turn(db)
    assert refresh(db)["kind"] == "waiting"
    del registry._running[live.id]
    receipt(receipts, 7, pid="proc_live")
    failed = refresh(db, "manual")
    assert (failed["kind"], failed["reason"], failed["presentation"]["stripe"]) == (
        "unresolved", "linked_work_unsuccessful", "attention")
    assert refresh(db, "notice") == failed  # wording, notices and time do not clear it
    # The failure predates the next genuine request; only that request's successful completion clears it.
    past = time.time() - 30
    receipt(receipts, 7, pid="proc_live", at=past)
    say(db, "user", "try again")
    assert refresh(db, "manual")["kind"] == "unresolved"
    say(db, "assistant", "ok")
    assert_done(refresh(db))


def test_current_process_without_exit_evidence_is_unknown(db, receipts):
    turn(db)
    receipt(receipts, None)
    assert refresh(db)["reason"] == "source_unknown"


def test_running_and_lost_subagents_are_never_green(db):
    db.create_session("child", source="subagent", parent_session_id="chat")
    turn(db)
    assert refresh(db)["kind"] == "waiting"
    silent = time.time() - so.SUBAGENT_LIVE_WINDOW_S - 60
    db._write_rowcount("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = 'child'", (silent, silent))
    db._write_rowcount("UPDATE messages SET timestamp = ? WHERE session_id = 'chat'", (silent - 60,))
    lost = refresh(db, "reap")
    assert (lost["kind"], lost["reason"]) == ("unresolved", "linked_work_unsuccessful")
    so.declare_outcome("chat", {"kind": "done", "summary": "everything is finished"}, db=db)
    assert refresh(db, "manual")["kind"] == "unresolved"


def test_failed_turn_is_attention_and_muted_wins(db):
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE
    say(db, "user", "do it")
    say(db, "assistant", FAILED_TURN_NOTICE, display_kind=FAILED_TURN_DISPLAY_KIND)
    status = refresh(db)
    assert (status["kind"], status["reason"]) == ("unresolved", "turn_failed")
    assert so.set_muted("chat", True, db=db)["status"]["kind"] == "muted"


def test_never_started_draft_has_no_stripe(db):
    status = refresh(db, "manual")
    assert (status["kind"], status["presentation"]["stripe"]) == ("unresolved", "none")


# ── tokened turn settlement (newest admission decides; tokenless keeps the transcript reading) ───────


def admitted_turn(db):
    home = db.db_path.parent
    turn(db)
    record_turn_start(home, "chat", "keep going")
    marker = read_turn_marker(home, "chat")
    assert refresh(db, "notice")["kind"] == "running"
    clear_turn_marker(home, "chat")
    return so.turn_identity(marker=marker)


def test_tokened_turn_settles_only_on_its_own_turn_end(db):
    own = admitted_turn(db)
    unsettled = refresh(db, "notice")
    assert (unsettled["kind"], unsettled["reason"]) == ("unresolved", "turn_unsettled")
    mismatched = refresh(db, "turn_end", concluding_turn=so.turn_identity(lease_holder="someone-else"))
    assert mismatched["reason"] == "turn_unsettled"
    assert refresh(db, "turn_end")["reason"] == "turn_unsettled"  # a token-less turn end is not its record
    assert_done(refresh(db, "turn_end", concluding_turn=own))
    assert_done(refresh(db, "notice"))


def test_tokened_turn_failure_is_attention(db):
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE
    own = admitted_turn(db)
    say(db, "user", "again")
    say(db, "assistant", FAILED_TURN_NOTICE, display_kind=FAILED_TURN_DISPLAY_KIND)
    status = refresh(db, "turn_end", concluding_turn=own)
    assert (status["kind"], status["reason"]) == ("unresolved", "turn_failed")


def test_newest_admission_decides_over_an_older_unsettled_turn(db):
    admitted_turn(db)
    assert refresh(db, "notice")["reason"] == "turn_unsettled"
    say(db, "user", "start over")
    say(db, "assistant", "ok")
    assert_done(refresh(db, "turn_end", concluding_turn=so.turn_identity(lease_holder="newer-turn")))


def test_tokenless_legacy_turn_keeps_the_transcript_reading(db):
    turn(db)
    assert_done(refresh(db, "turn_end"))
    say(db, "user", "and then?")
    assert refresh(db, "manual")["reason"] == "interrupted"


# ── fenced publication: a stale collector never overwrites its successor ─────────────────────────


def test_stale_collector_retries_against_the_successor(db, monkeypatch):
    import hermes_state_session_status_snapshot as snapshot

    turn(db)
    assert_done(refresh(db))
    real, calls = snapshot.canonical_status, []

    def racing(handle, tip, stored, **kw):
        status = real(handle, tip, stored, **kw)
        if not calls:
            so.declare_outcome("chat", {"kind": "done", "summary": "declared meanwhile"}, db=handle)
            say(handle, "user", "a newer request")
        calls.append(status["generation"])
        return status
    monkeypatch.setattr(snapshot, "canonical_status", racing)
    out = so.refresh_outcome("chat", "manual", db=db)
    assert len(calls) == 2 and calls[1] > calls[0]
    assert out["status"]["generation"] == calls[1] and out["status"]["kind"] == "unresolved"
    assert so.get_outcome("chat", db=db) == out


def test_exhausted_fence_leaves_the_successor_in_place(db, monkeypatch):
    import hermes_state_session_status_snapshot as snapshot

    turn(db)
    assert_done(refresh(db))
    real, count = snapshot.canonical_status, [0]

    def always_racing(handle, tip, stored, **kw):
        status = real(handle, tip, stored, **kw)
        count[0] += 1
        say(handle, "user", f"request {count[0]}")
        return status
    monkeypatch.setattr(snapshot, "canonical_status", always_racing)
    before = so.get_outcome("chat", db=db)
    out = so.refresh_outcome("chat", "manual", db=db)
    assert count[0] == snapshot.CAS_ATTEMPTS
    assert out == before == so.get_outcome("chat", db=db)


def test_one_refresh_reads_the_board_once_for_explanation_and_status(db, board, monkeypatch):
    """A second card read in the same refresh is a warm display read: it answers stale and starts a
    background projection, so the status would be reduced from different data than the explanation."""
    say(db, "user", "do it")
    say(db, "assistant", "Done.", finish_reason="stop")
    add_card(board, "t_open", "running")
    reads, background = [], []
    real = ksa.session_outcome_tasks
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **kw: reads.append(a) or real(*a, **kw))
    monkeypatch.setattr(ksa, "_refresh_links_in_background", background.append)

    first = refresh(db)
    assert len(reads) == 1 and background == []
    assert first["presentation"]["stripe"] == "waiting"

    set_card(board, "t_open", "done")  # the next refresh reads again: nothing is remembered across refreshes
    second = refresh(db)
    assert len(reads) == 2
    assert second["presentation"]["stripe"] == "done"


def test_a_failed_board_read_is_replayed_not_reread_as_empty(db, monkeypatch):
    say(db, "user", "do it")
    say(db, "assistant", "Done.", finish_reason="stop")
    calls = []

    def broken(*a, **kw):
        calls.append(a)
        raise OSError("board unreadable")

    monkeypatch.setattr(ksa, "session_outcome_tasks", broken)
    status = refresh(db)
    assert len(calls) == 1
    assert status["presentation"]["stripe"] != "done"
    assert status["reason"] == "source_unknown"
    assert any(reason.startswith("cards:") for reason in status["coverage"]["reasons"])


# ── row 3: real linked work and a human ask move the one status, one revision per change ─────────


def test_linked_work_and_ask_transitions_publish_one_revision_each(db, board):
    """waiting -> decision -> waiting -> done on the real card store; a refresh with no change publishes nothing."""
    turn(db)
    add_card(board, "t_work", "running")
    seen = [refresh(db)]
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', human_request = ? WHERE id = ?",
                     (json.dumps(HUMAN_REQUEST), "t_work"))
    ksa._reset_cache_for_tests()
    seen.append(refresh(db, "card_change"))
    set_card(board, "t_work", "running")
    seen.append(refresh(db, "card_change"))
    set_card(board, "t_work", "done")
    seen.append(refresh(db, "card_change"))
    assert [s["presentation"]["stripe"] for s in seen] == ["waiting", "stuck", "waiting", "done"]
    assert [s["revision"] for s in seen] == [1, 2, 3, 4]
    assert {s["generation"] for s in seen} == {seen[0]["generation"]}
    assert all(s["coverage"]["state"] == "complete" for s in seen)
    assert refresh(db, "manual") == seen[-1]
