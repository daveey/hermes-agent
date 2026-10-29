"""Notice guarantee (session-outcome contract, lane A2): a chat whose last message is a kanban notice older than
5 minutes, with no live turn, gets ONE close-out wake (budget 2/h); a wake that does not start, or a spent budget,
leaves the stored outcome ``unresolved{notice_tail}``; a notice about a card blocked on a human never wakes and
refreshes to ``decision``. [daveey]"""

from __future__ import annotations

import contextlib
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tui_gateway import notice_guarantee as ng

CHAT = "20261029_130000_a2guar"
HUMAN = {"action": "Reply ship or hold here. Unblock = deploy; archive = drop it.",
         "why_human": "Only David approves prod.", "interaction": "approval", "scope": "prod deploy",
         "evidence": "CI green", "choices": [{"label": "Ship", "value": "ship"}, {"label": "Hold", "value": "hold"}]}


@pytest.fixture
def db(tmp_path):
    so._reset_hooks_for_tests()
    ksa._reset_cache_for_tests()
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session(CHAT, "desktop")
    handle.append_message(CHAT, "user", "run the deploy card")
    handle.append_message(CHAT, "assistant", "Started the deploy card; I will report when it lands.")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def _notice(db, text, *, age=600.0, **meta):
    row = db.append_kanban_notice(CHAT, text, {"notice_id": f"n:{text}:{time.time()}", "display_text": text, **meta})
    db._write_rowcount("UPDATE messages SET timestamp = ? WHERE id = ?", (time.time() - age, row))
    return row


def _run(db, verdict="started"):
    calls = []

    def wake(sid, msg_id):
        calls.append((sid, msg_id))
        return verdict

    @contextlib.contextmanager
    def opener(chat_id):
        yield db, db.get_compression_tip(chat_id) or chat_id

    out = ng.guarantee_pass(stores=lambda: iter([db]), holders=list, session_db=opener, wake=wake,
                            spawn=lambda job: job())
    return out, calls


def _closeout(db, row):
    return db._read_one("SELECT json_extract(display_metadata, '$.closeout.state') FROM messages WHERE id = ?",
                        (row,))[0]


def test_an_unanswered_notice_gets_one_closeout_wake(db):
    row = _notice(db, "✔ Kanban t_aaa done — deploy card")

    out, calls = _run(db)

    assert calls == [(CHAT, row)] and [o["result"] for o in out] == ["dispatched"]
    assert _closeout(db, row) == "started"
    assert _run(db) == ([], [])  # claimed: never woken twice


def test_young_notice_waits_for_the_grace_window(db):
    _notice(db, "✔ Kanban t_aaa done — deploy card", age=30)
    assert _run(db) == ([], [])


def test_a_refused_wake_leaves_the_outcome_notice_tail(db):
    row = _notice(db, "✔ Kanban t_aaa done — deploy card")

    _out, calls = _run(db, verdict="refused")

    assert len(calls) == 1 and _closeout(db, row) == "refused"
    stored = so.get_outcome(CHAT, db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "notice_tail")


def test_a_spent_budget_refreshes_instead_of_waking(db):
    for n in range(ng.WAKES_PER_HOUR):
        _notice(db, f"earlier close-out {n}", closeout={"state": "started", "at": time.time() - 600})
    _notice(db, "✔ Kanban t_aaa done — deploy card")

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["refreshed:budget"]
    stored = so.get_outcome(CHAT, db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "notice_tail")


def test_a_blocked_on_human_notice_never_wakes_and_reads_decision(db):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="prod deploy", assignee="worker", session_id=CHAT,
                             initial_status="blocked", human_request=HUMAN, lint_blocked=False)
    _notice(db, f"⏸ Kanban {tid} blocked: needs approval — prod deploy", task_id=tid)
    ksa._reset_cache_for_tests()

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["refreshed:human"]
    stored = so.get_outcome(CHAT, db=db)
    assert stored["kind"] == "decision"
    assert stored["items"][0]["card_id"] == tid and [c["value"] for c in stored["items"][0]["choices"]] == ["ship", "hold"]
