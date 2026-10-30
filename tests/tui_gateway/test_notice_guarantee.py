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


# Remote notices (defect kanban-a9b5d2c7c1): a notice about a card on ANOTHER gateway carries that owner's board
# name, which does not exist here. The guarantee must not open a local board for it; it reads the
# ``blocked_on_human`` flag the remote writer stored. [daveey]

def _no_local_board(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a remote notice must not open a local kanban board")
    monkeypatch.setattr(kbc, "connect", refuse)


@pytest.mark.parametrize("flag, expected", [(True, "refreshed:human"), (False, "dispatched")])
def test_a_remote_notice_reads_its_flag_and_never_opens_a_local_board(db, monkeypatch, flag, expected):
    _no_local_board(monkeypatch)
    row = _notice(db, "⏸ Kanban t_remote1 on hub blocked — remote card", board="nosuchboard", task_id="t_remote1",
                  gateway="hub", kanban_event_keys=["remote:hub:nosuchboard/default/t_remote1:blocked@1:default:x"],
                  blocked_on_human=flag)

    out, calls = _run(db)

    assert [o["result"] for o in out] == [expected]
    assert (calls == []) is flag
    if not flag:
        assert _closeout(db, row) == "started"


def test_a_live_poller_row_of_only_remote_keys_is_remote_too(db, monkeypatch):
    _no_local_board(monkeypatch)
    _notice(db, "⏸ Kanban t_remote2 on hub blocked — remote card", blocked_on_human=True,
            kanban_event_keys=["remote:hub:bb/default/t_remote2:blocked@1:default:x"])

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["refreshed:human"]


def test_a_local_notice_whose_board_is_gone_still_gets_a_closeout(db):
    row = _notice(db, "✔ Kanban t_gone1 done — card on a deleted board", board="deletedboard", task_id="t_gone1")

    out, calls = _run(db)

    assert calls == [(CHAT, row)] and [o["result"] for o in out] == ["dispatched"]
    assert _closeout(db, row) == "started"


def test_a_notice_whose_handling_raises_is_stamped_error_once(db, monkeypatch):
    row = _notice(db, "✔ Kanban t_aaa done — deploy card")

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ng, "started_last_hour", boom)

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["error"]
    assert _closeout(db, row) == "error"
    assert ng.candidates(db, time.time()) == []  # handled once, not retried on every pass
    stored = so.get_outcome(CHAT, db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "notice_tail")


# Superseded decision notices (coach pattern closeout-notice-stale-decision): a "blocked" notice whose card has
# since moved on (the user answered and it was unblocked, it rescheduled itself, finished, or was archived) asks a
# question nobody needs to answer any more. It must not wake a close-out turn that re-asks it; it is stamped
# ``superseded`` and the chat's outcome is refreshed. [daveey]

def _decision_card():
    with contextlib.closing(kbc.connect()) as conn:
        return kb.create_task(conn, title="prod deploy", assignee="worker", session_id=CHAT,
                              initial_status="blocked", human_request=HUMAN, lint_blocked=False)


@pytest.mark.parametrize("move", ["unblock", "archive"])
def test_a_blocked_notice_whose_card_moved_on_is_superseded_not_woken(db, move):
    tid = _decision_card()
    row = _notice(db, f"⏸ [default] @default Kanban {tid} blocked: Decision needed — prod deploy", task_id=tid,
                  board="default")
    with contextlib.closing(kbc.connect()) as conn:
        assert (kb.unblock_task(conn, tid) if move == "unblock" else kb.archive_task(conn, tid))
    ksa._reset_cache_for_tests()

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["superseded"]
    assert _closeout(db, row) == "superseded"
    assert _run(db) == ([], [])  # handled once


def test_a_remote_blocked_notice_marked_stale_is_superseded(db, monkeypatch):
    _no_local_board(monkeypatch)
    row = _notice(db, "⏸ Kanban t_remote3 on hub blocked — remote card", board="nosuchboard", task_id="t_remote3",
                  gateway="hub", kanban_event_keys=["remote:hub:nosuchboard/default/t_remote3:blocked@1:default:x"],
                  blocked_on_human=True, stale=True, stale_tasks=["t_remote3"])

    out, calls = _run(db)

    assert calls == [] and [o["result"] for o in out] == ["superseded"]
    assert _closeout(db, row) == "superseded"


def test_a_blocked_notice_whose_card_is_still_blocked_by_an_agent_still_gets_a_closeout(db):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="flaky", assignee="worker", session_id=CHAT)
        kb.block_task(conn, tid, reason="transient CI outage", kind="transient")
    row = _notice(db, f"⏸ Kanban {tid} blocked: transient CI outage", task_id=tid, board="default")

    out, calls = _run(db)

    assert calls == [(CHAT, row)] and [o["result"] for o in out] == ["dispatched"]
