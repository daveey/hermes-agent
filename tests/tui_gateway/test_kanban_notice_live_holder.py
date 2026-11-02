"""A LIVE process's in-flight notice claim must not block the chat's owner forever. [daveey]

Live incident (t_01a199de3003e911, MacBook 2026-11-01): the completion of t_01a197b1039901c3 never reached chat
20261026_235722_bef6c6. Its sub row held ``inflight_from=203961, last_event_id=204381, inflight_pid=44404``: the
live ``hermes dashboard`` had claimed (203961, 204381] and never acked it. The chat's owner (desktop serve 51107)
could not claim while that live process held the row. The dashboard kept re-claiming the new events every 5 min,
was refused SESSION_NOT_OWNED, and rewound only its latest range, so its earlier marker stayed. Nine more rows
on the same pid were stuck the same way.

Fixed here: a refused non-owner releases its whole marker; a live holder's marker over a range with nothing
deliverable, or one held past NOTIFY_CLAIM_STALE_S, no longer blocks; and the sweeper and the poller ack a
silent-only claim (unblocked/archived) instead of leaking it.
"""

import contextlib
import os
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway import server
from tui_gateway import session_notifications as sn

KEY = "20261026_235722_bef6c6"


def _session(key: str = KEY, running: bool = False) -> dict:
    return {"session_key": key, "history_lock": threading.RLock(), "running": running}


def _task() -> str:
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Drill card", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=KEY)
        return tid


def _event(tid: str, kind: str) -> int:
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, {})
        return int(conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0])


def _complete(tid: str) -> None:
    with contextlib.closing(kbc.connect()) as conn:
        assert kb.complete_task(conn, tid, summary="reply OK and complete")


def _sub(tid: str) -> dict:
    with contextlib.closing(kbc.connect()) as conn:
        return dict(conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone())


def _hold(tid: str, *, inflight_from: int, last_event_id: int, pid: int, at) -> None:
    """A claim of (inflight_from, last_event_id] by ``pid`` that was never acked."""
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET inflight_from = ?, last_event_id = ?, inflight_pid = ?, "
                         "inflight_start = NULL, inflight_at = ? WHERE task_id = ?",
                         (inflight_from, last_event_id, pid, at, tid))


def _claim(tid: str) -> list:
    with contextlib.closing(kbc.connect()) as conn:
        return kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY, durable=True,
                                               kinds=sn._KANBAN_NOTIFY_KINDS,
                                               deliverable_kinds=sn._KANBAN_EVENT_FORMATTERS)[2]


def _as_other_live_process(monkeypatch):
    """From here on this process claims as a DIFFERENT live process (our parent's pid): a second backend."""
    monkeypatch.setattr(kbn, "_my_holder", lambda: (os.getppid(), None))


@pytest.fixture
def gate(monkeypatch):
    state = {"submitted": []}

    def _submit(rid, sid, session, text, **kwargs):
        state["submitted"].append((sid, text, kwargs))
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return state


@pytest.fixture
def sdb(tmp_path, monkeypatch):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(KEY, "desktop")
    d.append_message(KEY, "user", "run the drill")

    @contextlib.contextmanager
    def _opener(session):
        yield d

    monkeypatch.setattr(server, "_session_db", _opener)
    return d


def test_refused_live_holder_releases_its_whole_marker_and_the_owner_claims_next_tick(gate, sdb, monkeypatch):
    """The incident: the dashboard (this process, alive) holds an earlier never-acked claim, re-claims the new
    completion, is refused SESSION_NOT_OWNED and rewinds. Its whole marker must go, so the owner claims."""
    from hermes_cli.active_sessions import SESSION_NOT_OWNED, ActiveSessionRefusal

    refusal = ActiveSessionRefusal("This chat is active in another Hermes window/terminal.", SESSION_NOT_OWNED)
    monkeypatch.setattr(server, "_durable_owner_refusal", lambda session: None if session.get("_owner") else refusal)
    tid = _task()
    before = _sub(tid)["last_event_id"]
    blocked = _event(tid, "blocked")
    _hold(tid, inflight_from=before, last_event_id=blocked, pid=os.getpid(), at=int(time.time()))
    _complete(tid)

    server._notif_poll_kanban_scoped("dashboard", _session())
    assert gate["submitted"] == [], "the non-owner runs no turn"
    sub = _sub(tid)
    assert sub["inflight_from"] is None and sub["inflight_pid"] is None, "the whole marker is released"
    assert sub["last_event_id"] == before, "back to the earliest unacked cursor"

    _as_other_live_process(monkeypatch)  # the owner is another live process; the dashboard (our pid) lives on
    server._notif_poll_kanban_scoped("owner", {**_session(), "_owner": True})
    assert [s for s, _t, _k in gate["submitted"]] == ["owner"]
    assert tid in gate["submitted"][0][1]
    assert _sub(tid)["inflight_from"] is None


def test_live_holder_of_a_range_with_nothing_deliverable_does_not_block():
    """A live process leaked a claim over silent events only (unblocked): nothing will ever ack it."""
    tid = _task()
    before = _sub(tid)["last_event_id"]
    silent = _event(tid, "unblocked")
    _hold(tid, inflight_from=before, last_event_id=silent, pid=os.getppid(), at=int(time.time()))
    _complete(tid)

    kinds = [e.kind for e in _claim(tid)]
    assert "completed" in kinds, "the owner claims the completion past the leaked marker"
    assert _sub(tid)["inflight_pid"] != os.getppid()


@pytest.mark.parametrize("legacy", [False, True], ids=["inflight_at", "legacy-null-inflight_at"])
def test_stale_live_holder_marker_is_recovered(legacy):
    """A live holder that has sat on a deliverable claim past NOTIFY_CLAIM_STALE_S no longer blocks."""
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)
    with contextlib.closing(kbc.connect()) as conn:
        last = int(conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0])
        if legacy:  # written before inflight_at existed: dated by the newest event it covers
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?",
                             (kbn.NOTIFY_CLAIM_STALE_S + 60, tid))
    old = None if legacy else int(time.time()) - kbn.NOTIFY_CLAIM_STALE_S - 60
    _hold(tid, inflight_from=before, last_event_id=last, pid=os.getppid(), at=old)

    kinds = [e.kind for e in _claim(tid)]
    assert "completed" in kinds, "the held completion is redelivered by the claimer"


def test_fresh_live_holder_of_a_deliverable_claim_still_blocks():
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)
    with contextlib.closing(kbc.connect()) as conn:
        last = int(conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0])
    _hold(tid, inflight_from=before, last_event_id=last, pid=os.getppid(), at=int(time.time()))
    assert _claim(tid) == []
    assert _sub(tid)["inflight_pid"] == os.getppid()


def test_unloaded_sweep_acks_a_silent_only_claim(tmp_path, monkeypatch):
    """Why 4: the sweeper claimed a silent-only range (unblocked), had no text to write, and returned without an
    ack, leaving the row in flight under its live process for good."""
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    d = SessionDB(tmp_path / "state.db")
    d.create_session(KEY, "desktop")
    d.append_message(KEY, "user", "start")
    tid = _task()
    silent = _event(tid, "unblocked")

    @contextlib.contextmanager
    def opener(chat_id):
        yield (d, chat_id) if d.get_session(chat_id) else (None, None)

    assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []
    sub = _sub(tid)
    assert sub["last_event_id"] == silent and sub["inflight_from"] is None and sub["inflight_pid"] is None


def test_silent_claim_behind_a_pending_claim_is_acked_with_it(gate, sdb):
    """Why 4, the live poller: a silent-only claim made while an earlier claim of the same sub was still buffered
    skipped its ack and was dropped, so acking the earlier claim later left the silent range in flight."""
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _event(tid, "blocked")
    busy = _session(running=True)
    server._notif_poll_kanban_scoped("ui", busy)
    assert len(busy["_kanban_pending"]) == 1 and _sub(tid)["inflight_from"] == before

    silent = _event(tid, "unblocked")
    server._notif_poll_kanban_scoped("ui", busy)  # still mid-turn: the silent claim queues behind the first
    assert _sub(tid)["last_event_id"] == silent

    busy["running"] = False
    server._notif_poll_kanban_scoped("ui", busy)
    assert len(gate["submitted"]) == 1
    sub = _sub(tid)
    assert sub["inflight_from"] is None and sub["inflight_pid"] is None, "both claims acked"
