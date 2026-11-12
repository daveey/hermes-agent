"""Mutation-survivor tests: grace boundary, backfill eligibility/generation fences, notice coalescing."""
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_ask_resolved_backfill as bf
from hermes_cli import kanban_block_notice as bn
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_ask_backfill as legacy
from tests.hermes_cli import test_kanban_ask_resolved_check as helpers

board = helpers.board


def _fail(conn, tid, at):
    state = kwc.get_state(conn, tid)
    return kar.apply_result(conn, tid, state["cmd"], 255, "down", generation=state["generation"], now=at)


def test_grace_window_is_exactly_six_hours_with_count_already_met(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 255")
        assert _fail(conn, tid, 1000) is None
        assert _fail(conn, tid, 1001) is None
        assert _fail(conn, tid, 1000 + kar.BROKEN_MIN_SECONDS - 1) is None  # 3rd failure, one second short
        assert kwc.get_state(conn, tid)["fails"] == 3
        assert _fail(conn, tid, 1000 + kar.BROKEN_MIN_SECONDS) == kar.BROKEN_EVENT
    assert kar.BROKEN_MIN_SECONDS == 21600


# --- backfill eligibility ---------------------------------------------------------------------------------

def _candidates(conn, monkeypatch):
    seen = []
    real = bf._latest_drop
    monkeypatch.setattr(bf, "_latest_drop", lambda c, t: (seen.append(t), real(c, t))[1])
    bf.backfill(conn)
    return set(seen)


def test_sql_prefilter_excludes_probed_unblocked_and_already_marked(board, monkeypatch):
    with kbc.connect_closing() as conn:
        helpers._blocked_ask(conn, "exit 1")  # probe row present
        done = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, done, kar.BROKEN_EVENT)
        kb.unblock_task(conn, done)
        marked = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, marked, kar.BROKEN_EVENT)
        with kb.write_txn(conn):
            kar.mark_dropped(conn, marked, kar.BROKEN_EVENT, 5000)
        plain = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, plain, kar.LOST_EVENT)
        assert _candidates(conn, monkeypatch) == {plain}


def test_eligible_checks_each_clause(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        assert not bf._eligible(conn, tid)  # probe row present
        legacy._legacy(conn, tid, kar.BROKEN_EVENT)
        assert bf._eligible(conn, tid)
        assert not bf._eligible(conn, "missing")
        with kb.write_txn(conn):
            kar.mark_dropped(conn, tid, kar.BROKEN_EVENT, 5000)
        assert not bf._eligible(conn, tid)  # already marked
        plain = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, plain, kar.BROKEN_EVENT)
        with kb.write_txn(conn):  # blocked but the filed ask names no resolved_check
            conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?",
                         ('{"action": "x", "interaction": "user_terminal"}', plain))
        assert not bf._eligible(conn, plain)
        kb.unblock_task(conn, tid)
        assert not bf._eligible(conn, tid)


def test_foreign_card_without_host_is_left_and_with_own_host_is_marked(board, monkeypatch):
    with kbc.connect_closing() as conn:
        a = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, a, kar.BROKEN_EVENT)
        monkeypatch.setattr(kwc, "own_gateway", lambda c: "gw-home")
        monkeypatch.setattr(kwc, "foreign_ids", lambda c, own: {a})
        assert bf.backfill(conn) == []  # worker copy of another gateway's card, no probe_host
        b = helpers._blocked_ask(conn, "exit 1")
        with kb.write_txn(conn):
            kb._append_event(conn, b, "blocked", {"probe_host": "gw-home"})
        legacy._legacy(conn, b, kar.BROKEN_EVENT)
        monkeypatch.setattr(kwc, "foreign_ids", lambda c, own: {a, b})
        assert bf.backfill(conn) == [b]  # probe host is us, even though the card id is foreign


def test_write_fence_skips_when_latest_drop_changes_in_race(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, tid, kar.BROKEN_EVENT)
        real = bf._latest_drop

        def racing(c, t):
            result = real(c, t)
            if not racing.done:
                racing.done = True
                with kb.write_txn(c):  # newer filing, probe row still absent: only the drop comparison catches it
                    kb._append_event(c, t, "blocked", {})
            return result

        racing.done = False
        monkeypatch.setattr(bf, "_latest_drop", racing)
        assert bf.backfill(conn) == []
        assert "resolved_check_dropped_at" not in legacy._request(conn, tid)


def test_write_fence_skips_when_card_stops_being_eligible_in_race(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        legacy._legacy(conn, tid, kar.BROKEN_EVENT)
        real = bf._latest_drop

        def racing(c, t):
            result = real(c, t)
            if not racing.done:
                racing.done = True
                kb.unblock_task(c, t)  # unblock adds no blocked/drop/resolved event
            return result

        racing.done = False
        monkeypatch.setattr(bf, "_latest_drop", racing)
        assert bf.backfill(conn) == []
        assert "resolved_check_dropped_at" not in legacy._request(conn, tid)


def test_backfill_tolerates_a_database_without_wake_checks():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, human_request TEXT)")
    assert bf.backfill(conn) == []


def test_ask_filed_after_old_drop_with_older_filing_is_judged_by_latest_filing(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {})  # earlier filing
        legacy._legacy(conn, tid, kar.BROKEN_EVENT, at=5000)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {})  # latest filing, after the drop
        assert bf._latest_drop(conn, tid) is None
        assert bf.backfill(conn) == []


# --- notice coalescing ------------------------------------------------------------------------------------

def _ev(i, kind):
    return SimpleNamespace(id=i, kind=kind)


def test_coalesce_keeps_other_events_and_uses_latest_drop():
    batch = [_ev(1, "status"), _ev(2, "blocked"), _ev(3, "resolved_check_broken"), _ev(4, "blocked"),
             _ev(5, "resolved_check_lost"), _ev(6, "comment")]
    assert [e.id for e in bn.coalesce_superseded_blocks(batch)] == [1, 3, 5, 6]
    assert [e.id for e in bn.coalesce_superseded_blocks(batch[:3] + [_ev(4, "blocked")])] == [1, 3, 4]
    assert bn.coalesce_superseded_blocks(batch[:2]) == batch[:2]
    assert [e.id for e in bn.coalesce_superseded_blocks([_ev(1, "resolved_check_lost"), _ev(2, "blocked")])] == [1, 2]


def test_backfill_coalesces_only_exact_same_kind_references():
    broken, lost = kar.BROKEN_EVENT, kar.LOST_EVENT
    originals = [_ev(1, broken), _ev(2, broken), _ev(3, lost)]
    replay = SimpleNamespace(id=4, kind=broken, payload={"backfilled": True, "origin_event_id": 1})
    assert [e.id for e in bn.coalesce_superseded_blocks(originals + [replay])] == [2, 3, 4]
    for kind, payload in [("comment", replay.payload), (broken, None), (broken, []),
                          (broken, {"backfilled": False, "origin_event_id": 1}),
                          (broken, {"backfilled": True}),
                          (broken, {"backfilled": True, "origin_event_id": None}),
                          (broken, {"backfilled": True, "origin_event_id": "bad"}),
                          (broken, {"backfilled": True, "origin_event_id": 3})]:
        candidate = SimpleNamespace(id=4, kind=kind, payload=payload)
        batch = originals + [candidate]
        assert bn.coalesce_superseded_blocks(batch) == batch


def test_drop_warning_names_cause_from_mark_else_from_event():
    assert "its probe kept failing" in bn.drop_warning({"resolved_check_drop_reason": "resolved_check_broken"})
    assert "its probe went missing" in bn.drop_warning({}, "resolved_check_lost")
    assert "its probe kept failing" in bn.drop_warning({}, "resolved_check_broken")
    assert "()" not in bn.drop_warning({}) and "(its probe" not in bn.drop_warning({})


def test_store_request_clears_both_marks_without_mutating_the_callers_dict(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        original = legacy._request(conn, tid)
        original.update(resolved_check_dropped_at=5, resolved_check_drop_reason=kar.LOST_EVENT)
        passed = dict(original)
        with kb.write_txn(conn):
            kar.store_request(conn, tid, passed)
        stored = legacy._request(conn, tid)
        assert "resolved_check_dropped_at" not in stored and "resolved_check_drop_reason" not in stored
        assert passed == original  # the caller's request still carries the mark it handed in


def test_dropped_card_notice_names_the_work_and_refiled_card_via_session_formatter(board):
    from tui_gateway import session_notifications as sn

    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        with kb.write_txn(conn):
            kar.mark_dropped(conn, tid, kar.LOST_EVENT, 5000)
        marked = kb.get_task(conn, tid)
        text = sn._format_kanban_event_text({}, marked, SimpleNamespace(kind=kar.LOST_EVENT, payload={}), "default")
        assert f"Work: {marked.title}" in text and "Automatic detection stopped" in text
        with kb.write_txn(conn):
            kar.store_request(conn, tid, legacy._request(conn, tid))  # re-filed: mark cleared
        refiled = kb.get_task(conn, tid)
        text = sn._format_kanban_event_text({}, refiled, SimpleNamespace(kind=kar.LOST_EVENT, payload={}), "default")
        assert "re-filed since" in text.lower()
        text = sn._format_kanban_event_text({}, refiled, SimpleNamespace(kind="blocked", payload={}), "default")
        assert "re-filed since" not in text.lower()


def test_remote_drop_mark_requires_blocked_valid_nonzero_json_or_dict():
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway import kanban_remote_observations as kro

    request = {"resolved_check_dropped_at": 7}
    assert krn.drop_mark({"status": "blocked", "human_request": request}) == 7.0
    assert krn.drop_mark({"status": "blocked", "human_request": '{"resolved_check_dropped_at": 7}'}) == 7.0
    assert krn.drop_mark({"status": "done", "human_request": request}) is None
    assert krn.drop_mark({"status": "blocked", "human_request": {"resolved_check_dropped_at": 0}}) is None
    assert krn.drop_mark({"status": "blocked", "human_request": "not json"}) is None
    summary = kro._summary({"status": "blocked", "human_request": request})
    assert summary["human_request"] == {"resolved_check_dropped_at": 7.0}
    assert kro._summary({"status": "blocked", "human_request": {}})["human_request"] is None
