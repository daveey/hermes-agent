"""The unloaded-chat sweeper starts a turn instead of only writing a transcript row. [daveey]

2026-10-27: 19 chats sat idle on a kanban notice the sweeper had written as a ``kanban_notice`` row (5 of them held by
a live backend whose poller never claimed the event). A row is not a turn, so each chat waited for David to open it.
Now the sweeper runs ONE turn with the notice via ``server._wake_session_turn``; the passive row stays the fallback
when the wake can't start, the card is blocked on a human, the notice is diagnostic-only, the per-chat wake budget is
spent, or waking is disabled. A held chat whose poller missed the event is logged with its holder.
"""

import contextlib
import logging
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku

CHAT = "20261027_120000_abcdef"
LATER = time.time() + 3600
HUMAN_REQUEST = {
    "action": "Choose the deployment window.",
    "why_human": "The release owner must choose the acceptable downtime.",
    "interaction": "reply",
    "scope": "Staging deployment window",
    "evidence": "The release checklist has no owner-selected downtime window.",
}


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(CHAT, "desktop")
    d.append_message(CHAT, "user", "start the cards")
    d.append_message(CHAT, "assistant", "started")
    return d


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", raising=False)
    ku._reset_wake_budget_for_tests()
    yield
    ku._reset_wake_budget_for_tests()


def _opener(db):
    @contextlib.contextmanager
    def opener(chat_id):
        if db.get_session(chat_id) is None:
            yield None, None
        else:
            yield db, db.get_compression_tip(chat_id) or chat_id
    return opener


def _card(summary="shipped"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="unloaded card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _human_blocked_card():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="needs David", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="pick a window", kind="needs_input", human_request=HUMAN_REQUEST)
        return tid
    finally:
        conn.close()


def _notices(db):
    return [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]


class _Wake:
    def __init__(self, result="started"):
        self.result, self.calls = result, []

    def __call__(self, session_id, text, metadata):
        self.calls.append((session_id, text, metadata))
        return self.result


def _inline(job):
    return job()  # run the wake on the sweeper thread so the test sees its verdict


def _sweep(db, wake, **kw):
    kw.setdefault("holders", list)
    return ku.sweep_once(set(), now=LATER, session_db=_opener(db), wake=wake, spawn=_inline, **kw)


def test_finished_card_starts_a_turn_instead_of_a_passive_row(db):
    tid = _card()
    wake = _Wake("started")

    out = _sweep(db, wake)

    assert len(wake.calls) == 1
    session_id, text, metadata = wake.calls[0]
    assert session_id == CHAT and tid in text and "shipped" in text
    assert metadata["notice_id"].startswith(f"default:{tid}:") and metadata["task_id"] == tid
    assert _notices(db) == []  # the woken turn carries the notice; no duplicate row
    assert out[0]["woken"] is True
    assert _sweep(db, wake) == [] and len(wake.calls) == 1  # claimed once


@pytest.mark.parametrize("result", ["busy", "refused", "missing", "error"])
def test_a_wake_that_cannot_start_falls_back_to_the_passive_row(db, result):
    tid = _card()
    wake = _Wake(result)

    out = _sweep(db, wake)

    assert len(wake.calls) == 1
    notices = _notices(db)
    assert len(notices) == 1 and tid in notices[0]["content"]
    assert out[0]["woken"] is False and out[0]["wake_result"] == result


def test_a_raising_wake_falls_back_to_the_passive_row(db):
    _card()

    def boom(*_a):
        raise RuntimeError("backend gone")

    out = _sweep(db, boom)
    assert len(_notices(db)) == 1 and out[0]["woken"] is False and out[0]["wake_result"] == "error"


def test_card_blocked_on_a_human_is_not_woken(db):
    """The ask is David's: the chat is already red; a turn would only restate it."""
    _human_blocked_card()
    wake = _Wake()

    out = _sweep(db, wake)

    assert wake.calls == [] and len(_notices(db)) == 1
    assert out[0]["woken"] is False and out[0]["wake_result"] == "skipped:human"


def test_diagnostic_only_notice_is_not_woken(db, monkeypatch):
    from gateway.warning_notifications import DiagnosticText
    from tui_gateway import server

    _card()
    monkeypatch.setattr(server, "_format_kanban_event_text", lambda *a: DiagnosticText("worker heartbeat late"))
    wake = _Wake()

    out = _sweep(db, wake)

    assert wake.calls == [] and len(_notices(db)) == 1 and out[0]["wake_result"] == "skipped:diagnostic"


def test_per_chat_wake_budget_stops_create_notice_wake_loops(db, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", "2")
    wake = _Wake()
    for n in range(3):
        _card(summary=f"round {n}")
        _sweep(db, wake)

    assert len(wake.calls) == 2
    assert len(_notices(db)) == 1 and "round 2" in _notices(db)[0]["content"]


def test_waking_can_be_disabled(db, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    _card()
    wake = _Wake()

    out = _sweep(db, wake)

    assert wake.calls == [] and len(_notices(db)) == 1 and out[0]["wake_result"] == "skipped:disabled"


def test_chat_held_by_another_live_backend_logs_the_poller_miss(db, caplog):
    """A live holder's poller claims within ~5 s; reaching the sweeper means it missed. Name it."""
    tid = _card()
    holder = {"session_id": CHAT, "pid": 4242, "surface": "desktop", "host": "David-MBP"}

    with caplog.at_level(logging.WARNING, logger="tui_gateway.kanban_unloaded"):
        out = _sweep(db, _Wake(), holders=lambda: [holder])

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("poller miss" in m and CHAT in m and "4242" in m and tid in m for m in warned)
    assert out[0]["held_by"] == "pid 4242 (desktop@David-MBP)"


def test_unheld_chat_logs_no_poller_miss(db, caplog):
    _card()
    with caplog.at_level(logging.WARNING, logger="tui_gateway.kanban_unloaded"):
        out = _sweep(db, _Wake())
    assert not [r for r in caplog.records if "poller miss" in r.getMessage()]
    assert out[0]["held_by"] is None


def test_failed_wakes_do_not_spend_the_budget(db, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", "1")
    _card(summary="first")
    _sweep(db, _Wake("refused"))
    _card(summary="second")
    wake = _Wake("started")
    out = _sweep(db, wake)
    assert len(wake.calls) == 1 and out[0]["woken"] is True


def test_an_error_after_the_claim_still_delivers_the_row(db):
    """Claimed events must end as a turn or a row, never neither (a raising holder lookup lost them)."""
    tid = _card()

    def broken_holders():
        raise RuntimeError("registry corrupt")

    out = _sweep(db, _Wake(), holders=broken_holders)
    assert len(_notices(db)) == 1 and tid in _notices(db)[0]["content"]
    assert out[0]["woken"] is False


def test_wake_runs_off_the_sweeper_thread_and_a_failed_one_writes_the_row_there(db):
    """A wake may wait minutes on an agent build: the pass hands it to a worker and moves on."""
    import threading

    tid = _card()
    release = threading.Event()
    seen = []

    def slow_refused(session_id, text, metadata):
        release.wait(5)
        seen.append(session_id)
        return "refused"

    out = ku.sweep_once(set(), now=LATER, session_db=_opener(db), wake=slow_refused, holders=list)
    assert out[0]["wake_result"] == "dispatched" and _notices(db) == []  # the pass did not wait
    release.set()
    deadline = time.time() + 5
    while not _notices(db) and time.time() < deadline:
        time.sleep(0.05)
    assert seen == [CHAT] and len(_notices(db)) == 1 and tid in _notices(db)[0]["content"]


def test_no_free_wake_slot_writes_the_row_now(db, monkeypatch):
    monkeypatch.setattr(ku, "_wake_slots", ku.threading.BoundedSemaphore(1))
    assert ku._wake_slots.acquire(blocking=False)  # another chat's wake holds the only slot
    try:
        _card()
        wake = _Wake()
        out = _sweep(db, wake)
    finally:
        ku._wake_slots.release()
    assert wake.calls == [] and len(_notices(db)) == 1 and out[0]["wake_result"] == "skipped:slots"
