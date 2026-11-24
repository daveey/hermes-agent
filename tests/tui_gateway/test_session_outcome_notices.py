"""Session-outcome call sites on the notice paths (lane A2): a notice row written by the live poller or the
unloaded sweeper refreshes the chat's stored outcome; a linked card's status change refreshes it and marks the
card's earlier "blocked" notices stale; notices never fan out to released links, archived chats or chats moved
to another gateway. [daveey]"""

from __future__ import annotations

import contextlib
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_unloaded as ku
from tui_gateway import notice_fanout as nf
from tui_gateway import server

CHAT = "20261029_120000_a2chat"
LATER = time.time() + 3600


@pytest.fixture
def db(tmp_path, monkeypatch):
    so._reset_hooks_for_tests()
    nf._reset_for_tests()
    ksa._reset_cache_for_tests()
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")  # the passive-row path
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session(CHAT, "desktop")
    handle.append_message(CHAT, "user", "start the cards")
    handle.append_message(CHAT, "assistant", "Started the deploy card and subscribed this chat to its result.")
    yield handle
    handle.close()
    nf._reset_for_tests()
    so._reset_hooks_for_tests()


def _opener(db):
    @contextlib.contextmanager
    def opener(chat_id):
        if db.get_session(chat_id) is None:
            yield None, None
        else:
            yield db, db.get_compression_tip(chat_id) or chat_id
    return opener


def _card(*, done=True, chat=CHAT):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="deploy card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
        if done:
            kb.complete_task(conn, tid, summary="deployed")
        return tid


def _notices(db):
    return [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]


def test_live_poller_notice_write_refreshes_the_outcome(db, monkeypatch):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    so.refresh_outcome(CHAT, "manual", db=db)
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    _card()
    session = {"session_key": CHAT, "history": []}
    texts = server._collect_kanban_notifications(session)
    assert texts

    verdict, _fresh, _kwargs = server._kb_persist_batch(session, texts, False)

    assert verdict == "written"
    stored = so.get_outcome(CHAT, db=db)
    assert (stored["kind"], stored.get("reason")) == ("unresolved", "notice_tail")


def test_unloaded_sweeper_notice_write_refreshes_the_outcome(db):
    so.refresh_outcome(CHAT, "manual", db=db)
    _card()

    assert len(ku.sweep_once(set(), now=LATER, session_db=_opener(db))) == 1

    stored = so.get_outcome(CHAT, db=db)
    assert (stored["kind"], stored.get("reason")) == ("unresolved", "notice_tail")


def test_card_status_change_refreshes_and_marks_its_blocked_notice_stale(db):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="flaky deploy", assignee="worker", session_id=CHAT)
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        assert kb.block_task(conn, tid, reason="registry is down")
    ku.sweep_once(set(), now=LATER, session_db=_opener(db))  # writes the "⏸ … blocked" notice
    [blocked] = _notices(db)
    assert f"Kanban {tid} blocked" in blocked["content"]
    assert nf.card_change_pass(session_db=_opener(db)) == []  # first pass seeds the cursor
    ksa._reset_cache_for_tests()
    assert so.refresh_outcome(CHAT, "manual", db=db)["on"][0]["state"] == "blocked"

    with contextlib.closing(kbc.connect()) as conn:
        assert kb.schedule_task(conn, tid, reason="registry back at 14:00", clock_only=True,
                                awaiting={"kind": "clock", "why": "registry back at 14:00"})
    ksa._reset_cache_for_tests()
    changed = nf.card_change_pass(session_db=_opener(db))

    assert [(c["task_id"], c["status"], c["skipped"]) for c in changed] == [(tid, "scheduled", None)]
    stored = so.get_outcome(CHAT, db=db)
    # t_01a20c1b37830eaf: a clock park is waiting, with no running job claim.
    assert stored["kind"] == "waiting" and stored["on"][0]["state"] == "scheduled"
    assert stored["on"][0]["wait_kind"] == "clock"
    [row] = _notices(db)
    assert row["display_metadata"]["stale"] is True and row["display_metadata"]["stale_tasks"] == [tid]


@pytest.mark.parametrize("how", ["archived", "moved"])
def test_sweeper_skips_archived_and_moved_chats(db, how):
    tid = _card()
    if how == "archived":
        db.set_session_archived(CHAT, True)
    else:
        db.end_session(CHAT, "moved_to_gateway:hub")

    assert ku.sweep_once(set(), now=LATER, session_db=_opener(db)) == []
    assert _notices(db) == []
    with contextlib.closing(kbc.connect()) as conn:  # dropped, not left for the next pass
        assert kbn.unseen_events_for_sub(conn, kinds=("completed",), task_id=tid, platform="tui", chat_id=CHAT,
                                         thread_id="")[1] == []


def test_card_change_skips_archived_chats(db):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="card", assignee="worker", session_id=CHAT)
    nf.card_change_pass(session_db=_opener(db))
    db.set_session_archived(CHAT, True)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    assert [c["skipped"] for c in nf.card_change_pass(session_db=_opener(db))] == ["archived"]


def test_remote_notices_skip_released_links():
    task = {"id": "t_remote", "status": "blocked", "sessions": [
        {"gw": "mac", "rel": "shepherd", "profile": "default", "session": "kept"},
        {"gw": "mac", "rel": "shepherd", "profile": "default", "session": "gone", "released": 1}]}
    import json
    [(_owner, _key, _task, links)] = list(krn._sweep_cards([("hub", "default/default/t_remote", 1,
                                                              json.dumps(task))], "mac"))
    assert [link["session"] for link in links] == ["kept"]


def test_the_unloaded_sweeper_loop_runs_the_card_change_and_guarantee_passes(monkeypatch):
    import threading

    from tui_gateway import notice_guarantee as ng
    stop = threading.Event()
    ran = []
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: [])
    monkeypatch.setattr(krn, "sweep_remote_notices", lambda **k: [])
    monkeypatch.setattr(nf, "card_change_pass", lambda **k: ran.append("card_change") or [])
    monkeypatch.setattr(ng, "guarantee_pass", lambda **k: ran.append("guarantee") or stop.set() or [])

    ku._loop(stop)

    assert ran == ["card_change", "guarantee"]
