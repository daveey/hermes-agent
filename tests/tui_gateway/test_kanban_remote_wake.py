"""A remote card's notice wakes an idle chat, like a local card's, instead of only writing a row. [daveey]

2026-10-29, David-MBP chat 20261027_134353_5ab65c: hub cards t_01a1873edd84d3fc (done 15:23:42Z) and
t_01a18300e42c3364 (done 16:53:01Z) were written into the idle chat on time as passive ``kanban_notice`` rows
(15:24:56Z and 16:53:29Z, ledger delivered=1), but no turn ran until a LOCAL card's notice woke the chat at
18:58Z, 2-3.5 h later. The local unloaded sweep has woken the chat since 10-27 (``kanban_unloaded`` "a row is
not a turn"); the remote sweep in the same loop only ever wrote the row.

These tests drive the production sweeper loop (``kanban_unloaded._loop``) with the real replica, ledger and
state.db paths of the sandboxed home; only the agent turn (``wake_turn``) and the worker thread are faked.
"""

import contextlib
import json
import sqlite3
import threading
import time

import pytest

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_unloaded as ku

CHAT = "20261027_134353_5ab65c"


@pytest.fixture
def home(tmp_path, monkeypatch):
    hub = tmp_path / "hub-state"
    hub.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub))
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", raising=False)
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    with SessionDB(get_hermes_home() / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.append_message(CHAT, "user", "start the cards")
        db.append_message(CHAT, "assistant", "started")
    yield hub
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()


def _publish(hub, cards, arrived_ago=60.0):
    """The macbook replica: synchronized (cursor == head), hub-owned cards linked to CHAT, first seen on this
    host ``arrived_ago`` seconds ago (the durable arrival time both consumers share)."""
    with sqlite3.connect(hub / "hub.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS replica(gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, "
                     "data TEXT, updated_at INTEGER, PRIMARY KEY (gw, coll, key))")
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                         [("gateway", "macbook"), ("cursor", "100"), ("head", "100")])
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM replica").fetchone()[0]
        for card in cards:
            seq += 1
            conn.execute("INSERT OR REPLACE INTO replica VALUES ('hub','tasks',?,1,?,?,0)",
                         (f"default/default/{card['id']}", seq, json.dumps(card)))
    from tui_gateway.kanban_remote_observations import observe
    observe(hub / "hub.db", None, time.time() - arrived_ago)
    krn._reset_for_tests()  # a fresh process: only the durable ledger carries the arrival


def _done(tid, at, sessions=(CHAT,), summary="shipped the fix"):
    return {"id": tid, "status": "done", "title": f"card {tid}", "board": "default", "assignee": "default",
            "completed_at": at, "completion_notice": {"event_id": 81053, "created_at": at,
                                                      "payload": {"summary": summary}},
            "sessions": [{"gw": "macbook", "profile": "default", "session": s, "rel": "shepherd"} for s in sessions]}


def _blocked_on_david(tid, at):
    return {"id": tid, "status": "blocked", "title": f"card {tid}", "board": "default", "assignee": "default",
            "block_kind": "needs_input", "run": {"ended_at": at, "summary": "pick a window"},
            "human_request": {"action": "Reply A or B. Unblock = A; archive = drop.", "why_human": "owner pick",
                              "interaction": "reply", "scope": "deploy window", "evidence": "no window chosen"},
            "sessions": [{"gw": "macbook", "profile": "default", "session": CHAT, "rel": "shepherd"}]}


def _one_pass(monkeypatch, wake_verdict="started"):
    """One iteration of the production sweeper loop; returns the faked wakes it requested."""
    wakes = []

    def wake(tip, text, metadata):
        wakes.append((tip, text, metadata))
        return wake_verdict

    monkeypatch.setattr(ku, "wake_turn", wake)
    monkeypatch.setattr(ku, "_spawn_thread", lambda job: (job(), "dispatched")[1])
    stop = threading.Event()
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: stop.set() or [])
    ku._loop(stop)
    return wakes


def _notice_rows():
    with SessionDB(get_hermes_home() / "state.db") as db:
        return [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]


def _ledger(ref):
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM notices WHERE session=? AND ref=?", (CHAT, ref)).fetchone()
        return dict(row) if row else None


def test_remote_done_notice_wakes_an_idle_chat_once(home, monkeypatch):
    """Late-delivery path: the sweep starts a turn with the notice instead of leaving a silent row."""
    now = time.time()
    _publish(home, [_done("t_01a1873edd84d3fc", now - 75)])

    wakes = _one_pass(monkeypatch)

    assert [w[0] for w in wakes] == [CHAT]
    assert "t_01a1873edd84d3fc" in wakes[0][1] and "shipped the fix" in wakes[0][1]
    assert wakes[0][2]["kanban_event_keys"] == ["remote:hub:default/default/t_01a1873edd84d3fc:81053"]
    assert wakes[0][2]["woken"] is True
    assert _notice_rows() == []  # the started turn records the notice; no passive duplicate row
    assert _ledger("hub/default/default/t_01a1873edd84d3fc")["delivered"] == 1
    # Next pass (and a restarted backend): nothing is woken or written again.
    assert _one_pass(monkeypatch) == []
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    assert _one_pass(monkeypatch) == []


def test_remote_notice_gives_a_live_poller_the_grace_window_first(home, monkeypatch):
    """A state that arrived seconds ago belongs to the chat's live poller (any backend): the sweep neither
    claims it nor writes a row until the grace window has passed."""
    now = time.time()
    _publish(home, [_done("t_young", now - 2)], arrived_ago=2)

    assert _one_pass(monkeypatch) == []
    assert _notice_rows() == []
    assert _ledger("hub/default/default/t_young") is None
    # The poller can still claim it.
    got = krn.collect(CHAT, "default", now=now)
    assert [item["ref"] for item in got] == ["hub/default/default/t_young"]


def test_refused_wake_falls_back_to_one_passive_row(home, monkeypatch):
    now = time.time()
    _publish(home, [_done("t_refused", now - 120)])

    wakes = _one_pass(monkeypatch, wake_verdict="refused")

    assert len(wakes) == 1
    rows = _notice_rows()
    assert len(rows) == 1 and "t_refused" in rows[0]["content"]
    assert _ledger("hub/default/default/t_refused")["delivered"] == 1
    assert _one_pass(monkeypatch, wake_verdict="refused") == []
    assert len(_notice_rows()) == 1


def test_card_blocked_on_david_writes_the_row_without_waking(home, monkeypatch):
    now = time.time()
    _publish(home, [_blocked_on_david("t_needs_david", now - 120)])

    assert _one_pass(monkeypatch) == []
    rows = _notice_rows()
    assert len(rows) == 1 and "t_needs_david" in rows[0]["content"]
