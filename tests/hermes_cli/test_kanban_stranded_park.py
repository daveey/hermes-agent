"""No parked card stays invisible: the stranded-park backstop (``kanban_stranded_park``). [daveey]"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_block_policy as policy
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_stranded_park as stranded
from hermes_cli.kanban_ask_resolved import store_request
from hermes_cli.kanban_open_asks import card_is_open_human_ask
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_budget_hold as hold_helpers

kanban_home = helpers.kanban_home
stopped = hold_helpers.stopped


def _legacy_wrapper(conn, tid):
    """Explicit historical fixture: production no longer creates supervisor cards."""
    from tests.hermes_cli.test_kanban_spend_route_repair import _historical_wrapper
    sup = _historical_wrapper(conn, tid)
    decision.ensure_producer(conn, tid)
    return sup

OWN = "own-gw"
HOUR = 3600
DESK = f"{OWN}/default/desk-chat"


@pytest.fixture
def own_gateway(kanban_home, monkeypatch):
    """This gateway's name, on a board that carries the synced chat-link table (as a Board-synced board does)."""
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (OWN, {OWN, "remote"}))
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, "
                     "task_id TEXT NOT NULL, chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, "
                     "kind TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")


def _sweep(conn, **kw):
    """A working sweep: these tests exercise one pass each, so the per-board throttle is cleared first."""
    stranded._LAST_RUN.clear()
    return stranded.sweep_stranded_parks(conn, **kw)


def _session(home, sid, *, ended_ago=None, active_ago=None, **flags):
    """A chat in this gateway's state.db; ``ended_ago``/``active_ago`` in seconds before the real clock."""
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    try:
        if db.get_session(sid) is None:
            db.create_session(sid, source="desktop")
    finally:
        db.close()
    now = time.time()
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("UPDATE sessions SET ended_at=?, end_reason=?, last_activity_at=? WHERE id=?",
                     (None if ended_ago is None else now - ended_ago, None if ended_ago is None else "cli_close",
                      None if active_ago is None else now - active_ago, sid))
        for flag, value in flags.items():
            conn.execute(f"UPDATE sessions SET {flag}=? WHERE id=?", (int(value), sid))
        conn.commit()
    finally:
        conn.close()


def _link(conn, tid, gw, profile, session):
    from hermes_cli.kanban_chat_links import link_uid
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, ?, ?, ?, ?, 'created', 0, 0)",
                     (link_uid(tid, gw, profile, session, "created"), tid, gw, profile, session))


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _row(conn, tid):
    return conn.execute("SELECT status, block_kind, human_request FROM tasks WHERE id=?", (tid,)).fetchone()


def _later(hours):
    return int(time.time()) + int(hours * HOUR)


def _parked(conn, *, kind="transient", status="triage", event="agent_block_escalated", payload=None, capped=True):
    """A parked card. A triage card is ``capped`` by default: the stale-triage sweep stopped releasing it, so no
    agent route owns it (an uncapped one is the sweep's, see ``test_stale_triage_owned_card_is_untouched``)."""
    tid = kb.create_task(conn, title="Parked work", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?, block_kind=? WHERE id=?", (status, kind, tid))
        kb._append_event(conn, tid, event, payload or {"reason": "retry needed"})
        if status == "triage" and capped:
            kb._append_event(conn, tid, "triage_sweep_capped", {"cap": 3})
    return tid


def _spend_stopped_on_dead_chat(conn, home):
    """A spend stop routed to a then-live chat that later ended and went idle (the hub's stranded shape)."""
    tid = stopped(conn)
    _session(home, "human-chat")
    _link(conn, tid, OWN, None, "human-chat")
    assert budget.hold(conn, tid) == "triage"
    (notice,) = _events(conn, tid, decision.NOTICE)
    assert notice["producer"] == {"kind": "chats"}
    _session(home, "human-chat", ended_ago=2 * 86400, active_ago=2 * 86400)
    return tid


def test_spend_stop_on_dead_chat_gets_request_and_desk(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        cfg = {"kanban": {"triage_owner_chat": DESK}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == [tid]
        status, kind, raw = _row(conn, tid)
        assert status == "triage"
        assert card_is_open_human_ask(status, kind, raw)
        request = json.loads(raw)
        assert request["interaction"] == "approval" and "Situation: Bounded work" in request["scope"]
        assert request["action"].startswith("Decide how to handle: Bounded work")
        assert ("Unblock = release it to finish with 10 more runs under a goal-level scope "
                f"(`hermes kanban release {tid}`); safety holds still apply") in request["action"]
        assert "cannot release this hold" not in request["action"]
        assert "A) Unblock: Release this card to finish its acceptance: 10 more runs" in request["scope"]
        assert "Recommend: A - release it to finish" in request["scope"]
        from hermes_cli import kanban_release
        assert request["release_target"] == kanban_release.offer(conn, tid) and request["release_target"]["task"] == tid
        (escalated,) = _events(conn, tid, stranded.EVENT)  # the home's own record of the offer it filed
        assert escalated["release_target"] == request["release_target"]
        assert kanban_release.offered(conn, tid, request) == tid
        assert "without a bounded proposal" not in request["scope"]
        assert request["why_human"].startswith("Tried:") and "Only you can:" in request["why_human"]
        assert "spend_budget_triaged" in request["evidence"] and "runs=" in request["evidence"]
        assert "choices" not in request
        # The desk chat is bound: a synced shepherd link (no profile, as local reconcile wants it) and a tui sub.
        links = conn.execute("SELECT chat_gateway, chat_profile, chat_session, kind, released FROM task_chat_links "
                             "WHERE task_id=? AND kind='shepherd'", (tid,)).fetchall()
        assert [tuple(r) for r in links] == [(OWN, None, "desk-chat", "shepherd", 0)]
        (event,) = _events(conn, tid, stranded.EVENT)
        assert event["request"] is True and event["desk_chat"] == DESK
        assert event["reason"].startswith("spend_budget_triaged")
        _, seen = notify.unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id="desk-chat",
                                               kinds=(stranded.EVENT,))
        assert [e.kind for e in seen] == [stranded.EVENT]


@pytest.mark.parametrize("detail", [None, "Further work needs a new budget"])
def test_ask_has_summary_choices_consequences_recommendation(kanban_home, own_gateway, detail):
    with kbc.connect_closing() as conn:
        source = stopped(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body=? WHERE id=?", ("Fix the frozen sidebar in the Mac app.\nImplementation detail", source))
        assert budget.hold(conn, source) == "triage"
        sup = _legacy_wrapper(conn, source)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (sup,))
            kb._append_event(conn, sup, "decision_retained", {"disposition": {"summary": detail}})
            kb._append_event(conn, sup, "triage_sweep_capped", {"cap": 3})
        assert _sweep(conn, now=_later(3), cfg={}) == [sup]
        ask = json.loads(_row(conn, sup)[2])
        assert "Fix the frozen sidebar in the Mac app." in ask["scope"]
        assert (detail or "The decision owner kept the work stopped") in ask["scope"]
        assert "Situation:" in ask["scope"] and "Options:" in ask["scope"]
        assert "A)" in ask["scope"] and "B)" in ask["scope"] and "Recommend:" in ask["scope"]
        assert "If you do nothing" in ask["scope"]
        assert "no further spend" in ask["scope"]
        assert ask["why_human"].startswith("Tried:") and "Only you can:" in ask["why_human"]
        assert "decision_retained:" not in ask["scope"]
        assert "(no reason recorded)" not in str(ask)
        assert decision.pending(conn, source)


def test_dismissed_spend_episode_is_not_reasked_on_source(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        source = stopped(conn)
        assert budget.hold(conn, source) == "triage"
        sup = _legacy_wrapper(conn, source)
        assert kb.archive_task(conn, sup)
        # Even before producer reconciliation, archive is already a disposition.
        assert _sweep(conn, now=_later(3), cfg={}) == []
        decision.ensure_producer(conn, source)
        assert _sweep(conn, now=_later(6), cfg={}) == []
        assert _row(conn, source)[2] is None
        assert decision.pending(conn, source)


def test_remote_desk_is_a_hub_subscription(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        cfg = {"kanban": {"triage_owner_chat": "remote/default/desk"}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == [tid]
        subs = conn.execute("SELECT platform, chat_id FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchall()
        assert [tuple(r) for r in subs] == [("hub", "remote/default/desk")]
        links = conn.execute("SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links "
                             "WHERE task_id=? AND kind='shepherd'", (tid,)).fetchall()
        assert [tuple(r) for r in links] == [("remote", "default", "desk")]


def test_young_park_untouched_and_comment_does_not_reset_clock(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(1), cfg={}) == []
        assert _row(conn, tid)[2] is None
        kb.add_comment(conn, tid, "someone", "still looking")
        later = _later(3)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events "
                         "WHERE task_id=?)", (later - 1800, tid))
        assert _sweep(conn, now=later, cfg={}) == [tid]


@pytest.mark.parametrize("where", ["own-live", "own-ended-recent", "remote"])
def test_live_chat_owner_leaves_card_alone(kanban_home, own_gateway, where):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        if where == "remote":
            _link(conn, tid, "remote", "default", "their-chat")
        else:
            ended = 3600 if where == "own-ended-recent" else None
            _session(kanban_home, "live-chat", ended_ago=ended, active_ago=ended)
            _link(conn, tid, OWN, None, "live-chat")
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, tid)[2] is None


@pytest.mark.parametrize("flag", ["archived", "auto_archived", "hidden"])
def test_archived_or_hidden_chat_is_no_owner(kanban_home, own_gateway, flag):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        _session(kanban_home, "gone-chat", **{flag: 1})
        _link(conn, tid, OWN, None, "gone-chat")
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]


def test_open_decision_card_owns_until_done(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        assert budget.hold(conn, tid) == "triage"
        sup = _legacy_wrapper(conn, tid)
        assert stranded.open_decision_card(conn, tid) == sup
        assert _sweep(conn, now=_later(3), cfg={}) == []
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (sup,))
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert not card_is_open_human_ask(*_row(conn, tid))


def _reparked_supervisor(conn):
    """The hub shape of 2026-11-22/23: a spend-stopped source escalated while ownerless (its supervisor done), then
    a new supervisor opened for the same episode, which itself parks with no owner."""
    tid = stopped(conn)
    assert budget.hold(conn, tid) == "triage"
    first = _legacy_wrapper(conn, tid)
    with kb.write_txn(conn):
        conn.execute("DELETE FROM tasks WHERE id=?", (first,))
    assert _sweep(conn, now=_later(3), cfg={}) == [tid]
    sup = _legacy_wrapper(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', block_kind='transient' WHERE id=?", (sup,))
        kb._append_event(conn, sup, "agent_block_escalated", {"reason": "decision retained"})
        kb._append_event(conn, sup, "triage_sweep_capped", {"cap": 3})
    return tid, sup


def test_supervisor_of_an_asking_source_is_not_asked_again(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid, sup = _reparked_supervisor(conn)
        assert card_is_open_human_ask(*_row(conn, tid))
        assert _sweep(conn, now=_later(6), cfg={}) == []
        assert not card_is_open_human_ask(*_row(conn, sup))
        assert _events(conn, sup, stranded.EVENT) == []


def test_supervisor_is_asked_once_its_source_ask_is_gone(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid, sup = _reparked_supervisor(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=NULL, block_kind='transient' WHERE id=?", (tid,))
        assert sup in _sweep(conn, now=_later(6), cfg={})
        assert card_is_open_human_ask(*_row(conn, sup))


@pytest.mark.parametrize("answer", ["promoted", "archived"])
def test_supervisor_of_an_answered_source_is_not_asked(kanban_home, own_gateway, answer):
    with kbc.connect_closing() as conn:
        tid, sup = _reparked_supervisor(conn)
        if answer == "promoted":
            ok, err = kb.promote_task(conn, tid, actor="david", reason="re-scoped")
            assert ok, err
        else:
            assert kb.archive_task(conn, tid)
        assert sup not in _sweep(conn, now=_later(6), cfg={})
        assert not card_is_open_human_ask(*_row(conn, sup))


def test_leftover_supervisor_of_an_earlier_stop_is_not_asked(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid, sup = _reparked_supervisor(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=NULL, block_kind='transient' WHERE id=?", (tid,))
        old = kb.create_task(conn, title="Spend budget decision: earlier stop", assignee="default",
                             created_by="kanban-spend-budget", idempotency_key=f"spend-budget-decision:{tid}:1")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage', block_kind='transient' WHERE id=?", (old,))
            kb._append_event(conn, old, "agent_block_escalated", {"reason": "decision retained"})
            kb._append_event(conn, old, "triage_sweep_capped", {"cap": 3})
        assert stranded.open_decision_card(conn, tid) == sup
        escalated = _sweep(conn, now=_later(6), cfg={})
        assert sup in escalated and old not in escalated


def test_transient_triage_card_is_rekinded(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn, kind="transient")
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        status, kind, raw = _row(conn, tid)
        assert kind == "needs_input"
        assert card_is_open_human_ask(status, kind, raw)
        assert "retry needed" in json.loads(raw)["scope"]


def test_created_parked_card_uses_creation_as_episode(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Straight into triage", assignee="default", triage=True)
        assert _row(conn, tid)[0] == "triage"
        assert stranded.park_entry(conn, tid)["kind"] == "created"
        # Never swept: the stale-triage sweep owns it.
        assert _sweep(conn, now=_later(3), cfg={}) == []
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "triage_sweep_capped", {"cap": 3})
        assert stranded.park_entry(conn, tid)["kind"] == "created"
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]


def _dependency_wait(conn, mp):
    parent = kb.create_task(conn, title="parent", assignee="default")
    tid = kb.create_task(conn, title="child", assignee="default", parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='dependency' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "blocked", {"reason": "waits on parent"})
    return tid


def _blocked(conn, *, kind="transient", sql=None):
    tid = _parked(conn, kind=kind, status="blocked", event="blocked")
    if sql:
        with kb.write_txn(conn):
            conn.execute(sql, (tid,))
    return tid


def _foreign(conn, mp):
    from hermes_cli import kanban_wake_check
    tid = _blocked(conn)
    mp.setattr(kanban_wake_check, "foreign_ids", lambda conn, own=None: {tid})
    return tid


_EXCLUDED = {
    "dependency": _dependency_wait,
    "time": lambda conn, mp: _blocked(conn, kind="time"),
    "wake_check": lambda conn, mp: _blocked(conn, sql="INSERT INTO task_wake_checks (task_id, cmd, every, next_at, "
                                                       "fails, generation, purpose) VALUES (?, 'true', 600, 0, 0, 'g', 'wake')"),
    "running": lambda conn, mp: _blocked(conn, sql="UPDATE tasks SET current_run_id=12345 WHERE id=?"),
    "claimed": lambda conn, mp: _blocked(conn, sql="UPDATE tasks SET claim_lock='host:1' WHERE id=?"),
    "foreign": _foreign,
}


@pytest.mark.parametrize("case", sorted(_EXCLUDED))
def test_excluded_cards_are_untouched(kanban_home, own_gateway, monkeypatch, case):
    with kbc.connect_closing() as conn:
        tid = _EXCLUDED[case](conn, monkeypatch)
        before = _row(conn, tid)
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, tid) == before
        assert _events(conn, tid, stranded.EVENT) == []


def test_dependency_with_closed_parents_is_escalated(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        tid = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
            conn.execute("UPDATE tasks SET status='blocked', block_kind='dependency' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "blocked", {"reason": "waits on parent"})
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert card_is_open_human_ask(*_row(conn, tid))
        assert _row(conn, tid)[1] == "needs_input"


def test_contained_card_gets_desk_link_only(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_containment_connections as containment
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        monkeypatch.setattr(containment, "held", lambda conn, task_id: task_id == tid)
        before = _row(conn, tid)
        cfg = {"kanban": {"triage_owner_chat": DESK}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == [tid]
        assert _row(conn, tid) == before
        assert conn.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id=? AND kind='shepherd'",
                            (tid,)).fetchone()[0] == 1
        (event,) = _events(conn, tid, stranded.EVENT)
        assert event["request"] is False and event["desk_chat"] == DESK
        assert _sweep(conn, now=_later(3), cfg=cfg) == []


def test_once_per_episode_and_again_on_a_new_episode(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        # The ask was cleared (an agent answered it) but the card stays parked in the same episode: no repeat.
        with kb.write_txn(conn):
            store_request(conn, tid, None)
        assert _sweep(conn, now=_later(3), cfg={}) == []
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {"reason": "parked again"})
            kb._append_event(conn, tid, "triage_sweep_capped", {"cap": 3})
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert len(_events(conn, tid, stranded.EVENT)) == 2
        assert "parked again" in json.loads(_row(conn, tid)[2])["scope"]


def test_thresholds_come_from_config(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        cfg = {"kanban": {"stranded_park_seconds": 4 * HOUR}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == []
        assert _sweep(conn, now=_later(5), cfg=cfg) == [tid]
    assert stranded.settings({}) == (stranded.DEFAULT_PARK_SECONDS, stranded.DEFAULT_OWNER_IDLE_SECONDS, "",
                                     stranded.DEFAULT_SWEEP_INTERVAL_SECONDS, stranded.DEFAULT_STALE_DEFER_SECONDS)


def test_sweep_failure_never_breaks_the_tick(kanban_home, monkeypatch, caplog):
    def boom(conn, **_):
        raise RuntimeError("stranded boom")

    monkeypatch.setattr(stranded, "sweep_stranded_parks", boom)
    result = SimpleNamespace(agent_block_escalated=[], ask_rechecked=[])
    with kbc.connect_closing() as conn, caplog.at_level(logging.WARNING):
        policy.sweep_blocked_cards(conn, result)
    record = next(r for r in caplog.records if "stranded-park sweep failed" in r.getMessage())
    assert record.exc_info is not None


def test_dispatcher_tick_runs_the_sweep(kanban_home, own_gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(stranded, "sweep_stranded_parks", lambda conn, **_: calls.append(conn) or [])
    result = SimpleNamespace(agent_block_escalated=[], ask_rechecked=[])
    with kbc.connect_closing() as conn:
        policy.sweep_blocked_cards(conn, result)
    assert len(calls) == 1


def test_sweep_is_throttled_per_board(kanban_home, own_gateway, monkeypatch):
    """At most one working pass per kanban.stranded_sweep_interval_seconds: a second call inside the interval is a
    no-op (no escalation, no ownership reads); after the interval it runs again."""
    monkeypatch.setattr(stranded, "_LAST_RUN", {})
    with kbc.connect_closing() as conn:
        first = _later(3)
        assert stranded.sweep_stranded_parks(conn, now=first, cfg={}) == []
        tid = _parked(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=created_at-? WHERE task_id=?", (4 * HOUR, tid))
        cfg = {"kanban": {"stranded_sweep_interval_seconds": 600}}
        assert stranded.sweep_stranded_parks(conn, now=first + 599, cfg=cfg) == []
        assert _row(conn, tid)[2] is None
        assert stranded.sweep_stranded_parks(conn, now=first + 600, cfg=cfg) == [tid]
        # The default interval applies without config.
        assert stranded.sweep_stranded_parks(conn, now=first + 600 + 299, cfg={}) == []


def test_state_db_opened_once_per_sweep(kanban_home, own_gateway, monkeypatch):
    opened = []
    real = stranded._open_store
    monkeypatch.setattr(stranded, "_open_store", lambda path: opened.append(path) or real(path))
    with kbc.connect_closing() as conn:
        tids = [_parked(conn) for _ in range(3)]
        _session(kanban_home, "live-chat")
        _session(kanban_home, "dead-chat", archived=1)
        for tid, chat in zip(tids, ("live-chat", "dead-chat", "dead-chat")):
            _link(conn, tid, OWN, None, chat)
        assert sorted(_sweep(conn, now=_later(3), cfg={})) == sorted(tids[1:])
    assert len(opened) == 1 and opened[0].name == "state.db"


# --- review fixes: agent-owned parks stay agent work; the ask is answerable -----------------------------------------

def test_stale_triage_owned_card_is_untouched(kanban_home, own_gateway):
    """A triage card the stale-triage sweep will still release (re-entered since its last sweep, not capped) is
    agent-owned even when the sweep has not run (it only runs while the dispatcher may spawn)."""
    from hermes_cli.kanban_triage_sweep import awaiting_stale_sweep
    with kbc.connect_closing() as conn:
        tid = _parked(conn, capped=False)
        assert awaiting_stale_sweep(conn, tid)
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, tid)[2] is None
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "triage_sweep_capped", {"cap": 3})
        assert not awaiting_stale_sweep(conn, tid)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]


def test_open_recovery_card_owns_the_park(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_resume_requester as requester
    with kbc.connect_closing() as conn:
        tid = _parked(conn, status="blocked", event="continuation_exhausted", kind=None)
        rec = kb.create_task(conn, title="recover", assignee="default")
        monkeypatch.setattr(requester, "open_recovery_card", lambda c, target: rec if target == tid else None)
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, tid)[2] is None
        monkeypatch.setattr(requester, "open_recovery_card", lambda c, target: None)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]


def _goal_parked(conn):
    """A real judged goal park (goal_unachievable, re-kinded blocked event) and its kernel recovery card."""
    from hermes_cli.kanban_goal_recovery import current_park, park
    from hermes_cli.kanban_resume_requester import ensure_recovery_cards
    tid = kb.create_task(conn, title="Deliver exact goal", assignee="default", goal_mode=True)
    assert kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert park(conn, task, "Prerequisite unavailable", expected_run_id=task.current_run_id)
    assert kb.get_task(conn, tid).status == "triage" and current_park(conn, tid)
    (rec,) = ensure_recovery_cards(conn)
    return tid, rec


def test_goal_park_with_open_recovery_card_is_untouched(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid, _ = _goal_parked(conn)
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, tid)[2] is None


def test_goal_park_whose_recovery_finished_is_escalated(kanban_home, own_gateway):
    """current_park stays true after the goal recovery card completes (its brief leaves the source parked), so it
    owns nothing: the source is escalated once parked past the threshold."""
    from hermes_cli.kanban_goal_recovery import current_park
    with kbc.connect_closing() as conn:
        tid, rec = _goal_parked(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (rec,))
        assert current_park(conn, tid)
        assert _sweep(conn, now=_later(1), cfg={}) == []
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        status, kind, raw = _row(conn, tid)
        assert card_is_open_human_ask(status, kind, raw)


def test_lane_gate_and_shared_blocker_parks_are_untouched(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        gate = _parked(conn, kind="lane_gate")
        shared = _parked(conn, status="blocked", event="blocked",
                         payload={"reason": "same outage", "blocker_id": "t_0123456789abcdef"})
        assert _sweep(conn, now=_later(3), cfg={}) == []
        assert _row(conn, gate)[2] is None and _row(conn, shared)[2] is None


def test_desk_subscription_is_written_only_with_the_escalation(kanban_home, own_gateway, monkeypatch):
    """The desk sub is inside the escalation txn: a card that stops qualifying at the re-check gets no sub."""
    calls = []
    real = stranded._escalated

    def flip(conn, task_id, entry):
        calls.append(task_id)
        return len(calls) > 1 or real(conn, task_id, entry)

    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        monkeypatch.setattr(stranded, "_escalated", flip)
        assert _sweep(conn, now=_later(3), cfg={"kanban": {"triage_owner_chat": DESK}}) == []
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id=?", (tid,)).fetchone()[0] == 0


def test_generated_ask_passes_the_ask_lint(kanban_home, own_gateway):
    from hermes_cli.kanban_block_policy import standing_ask_error, user_ask_lint_error
    with kbc.connect_closing() as conn:
        plain = _parked(conn)
        spend = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert sorted(_sweep(conn, now=_later(3), cfg={})) == sorted([plain, spend])
        for tid in (plain, spend):
            request = json.loads(_row(conn, tid)[2])
            assert user_ask_lint_error(request) is None, request["action"]
            assert standing_ask_error(conn, tid, "Parked work", request) is None
        assert "Unblock = it goes back to work" in json.loads(_row(conn, plain)[2])["action"]


def _answer(conn, tid, choice):
    from hermes_cli import kanban_card_answer as ca
    request = {"ownerGateway": "owner", "ownerProfile": "default", "board": "default", "task": tid,
               "generation": ca.snapshot(conn, tid)["generation"], "choice": choice,
               "operationId": f"desktop-op-{choice}"}
    return ca.apply(conn, request, owner_gateway="owner", owner_profile="default", board="default",
                    verify_source=lambda req: {"gateway": "origin", "recorded": True, "answer": req})


def test_archive_answer_archives_and_unblock_answer_resumes(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        archived, resumed = _parked(conn), _parked(conn)
        assert sorted(_sweep(conn, now=_later(3), cfg={})) == sorted([archived, resumed])
        _answer(conn, archived, "archive")
        assert kb.get_task(conn, archived).status == "archived"
        _answer(conn, resumed, "unblock")
        task = kb.get_task(conn, resumed)
        assert task.status not in ("triage", "blocked", "archived") and task.human_request is None


def test_spend_pending_remote_unblock_answer_releases_the_card(kanban_home, own_gateway):
    """The ask on a spend-stopped card names the one-step release, and the remote Desktop Unblock answer
    (kanban_card_answer) is that release, recorded as the human act the verified Desktop operation is."""
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert f"`hermes kanban release {tid}`" in json.loads(_row(conn, tid)[2])["action"]
        receipt = _answer(conn, tid, "unblock")
        assert receipt["operationId"] == "desktop-op-unblock" and receipt["choice"] == "unblock"
        assert kb.get_task(conn, tid).status == "ready" and not decision.pending(conn, tid)
        (mandate,) = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'", (tid,))]
        assert mandate.endswith("Desktop Unblock, operation desktop-op-unblock")
        written = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,))]
        assert any(p.get("writer_kind") == "human" for p in written)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='card_answer_released'",
                            (tid,)).fetchone()[0] == 1


def _retry(conn, tid, generation):
    """The same Desktop operation delivered again, with the generation it was recorded against."""
    from hermes_cli import kanban_card_answer as ca
    request = {"ownerGateway": "owner", "ownerProfile": "default", "board": "default", "task": tid,
               "generation": generation, "choice": "unblock", "operationId": "desktop-op-unblock"}
    return ca.apply(conn, request, owner_gateway="owner", owner_profile="default", board="default",
                    verify_source=lambda req: {"gateway": "origin", "recorded": True, "answer": req})


def test_remote_unblock_retry_returns_the_receipt_and_a_lost_receipt_is_recorded(kanban_home, own_gateway):
    from hermes_cli import kanban_card_answer as ca
    from hermes_cli import kanban_release
    with kbc.connect_closing() as conn:
        done, crashed = _spend_stopped_on_dead_chat(conn, kanban_home), _spend_stopped_on_dead_chat(conn, kanban_home)
        assert sorted(_sweep(conn, now=_later(3), cfg={})) == sorted([done, crashed])
        generation = ca.snapshot(conn, done)["generation"]
        receipt = _answer(conn, done, "unblock")
        assert _retry(conn, done, generation) == receipt
        # The release landed and the process died before the receipt: the retry records it, releasing nothing more.
        generation = ca.snapshot(conn, crashed)["generation"]
        out = kanban_release.release(conn, crashed, writer=kanban_release.human_writer("desktop-operation:x"),
                                     author="user-steps", note=kanban_release.desktop_note("desktop-op-unblock"))
        assert out["outcome"] == "released"
        recorded = _retry(conn, crashed, generation)
        assert recorded["operationId"] == "desktop-op-unblock" and _retry(conn, crashed, generation) == recorded
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'",
                            (crashed,)).fetchone()[0] == 1


def test_remote_custom_choice_answer_on_a_held_card_is_unchanged(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_card_answer as ca
    from hermes_cli import kanban_release
    monkeypatch.setattr(kanban_release, "release", lambda *a, **kw: pytest.fail("custom choices never release"))
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        ask = json.loads(_row(conn, tid)[2])
        ask["choices"] = [{"label": "Unblock", "value": "unblock"}, {"label": "Later", "value": "later"}]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(ask), tid))
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held|spend_decision_pending"):
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and decision.pending(conn, tid)


def test_remote_unblock_refused_by_safety_leaves_the_ask_and_records_the_reason(kanban_home, own_gateway):
    from hermes_cli import kanban_card_answer as ca
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        stop = decision.pending_episode(conn, tid)  # a refusal that arrived after the ask offered the release
        kb._append_event(conn, tid, "decision_hold_restricted",
                         {"cause": "safety_refusal", "episode": stop.get("uid") or str(stop["id"])})
        conn.commit()
        for _ in range(2):  # a redelivery says it once
            with pytest.raises(ca.AnswerRefused, match="release_refused: safety_restricted"):
                _answer(conn, tid, "unblock")
        status, _, raw = _row(conn, tid)
        assert status == "triage" and json.loads(raw)["action"]
        notes = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND author='user-steps'", (tid,))]
        assert len(notes) == 1 and "did not release" in notes[0] and "desktop-op-unblock" in notes[0]
        assert not conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'",
                                (tid,)).fetchone()


# --- the stale-sweep deferral is bounded -----------------------------------------------------------------------------

def test_busy_card_waits_for_stale_sweep_only_up_to_the_defer_bound(kanban_home, own_gateway):
    """A re-entered, uncapped triage card that gets a comment every 30 min is never idle for sweep_stale_triage, so
    waiting on that sweep would defer it forever: it is escalated once its park passes 6 h, not before."""
    from hermes_cli.kanban_triage_sweep import awaiting_stale_sweep, sweep_stale_triage
    with kbc.connect_closing() as conn:
        tid = _parked(conn, capped=False)
        start = int(time.time())
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (start, tid))
        for k in range(1, 17):  # every 30 min for 8 h
            kb.add_comment(conn, tid, "someone", f"still looking ({k})")
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events "
                             "WHERE task_id=?)", (start + k * 1800, tid))
        assert awaiting_stale_sweep(conn, tid)
        assert sweep_stale_triage(conn, now=start + 8 * HOUR) == []  # never idle an hour
        assert _sweep(conn, now=start + 3 * HOUR, cfg={}) == []
        assert _sweep(conn, now=start + 6 * HOUR - 60, cfg={}) == []
        assert _sweep(conn, now=start + 6 * HOUR, cfg={}) == [tid]
        assert card_is_open_human_ask(*_row(conn, tid))


def test_defer_bound_comes_from_config(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn, capped=False)
        cfg = {"kanban": {"stranded_stale_defer_seconds": 4 * HOUR}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == []
        assert _sweep(conn, now=_later(4.1), cfg=cfg) == [tid]


def test_invalid_human_request_is_not_left_to_the_stale_sweep(kanban_home, own_gateway):
    """sweep_stale_triage skips any card with a stored human_request, so an invalid one is not the sweep's: it is
    escalated after the normal 2 h and replaced by a valid request."""
    from hermes_cli.kanban_triage_sweep import awaiting_stale_sweep
    with kbc.connect_closing() as conn:
        tid = _parked(conn, capped=False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps({"action": "half an ask"}), tid))
        assert not card_is_open_human_ask(*_row(conn, tid))
        assert not awaiting_stale_sweep(conn, tid)
        assert _sweep(conn, now=_later(1), cfg={}) == []
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert card_is_open_human_ask(*_row(conn, tid))


# --- the Unblock click releases only an ask that offered it (kanban_release.offered) ------------------------------

def _restamp(conn, tid, change):
    """Rewrite the swept ask as ``change`` leaves it; the lifecycle (and so the generation's events) is untouched."""
    ask = json.loads(_row(conn, tid)[2])
    change(ask)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(ask), tid))


def _no_release(conn, tid):
    return not conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'",
                            (tid,)).fetchone() and decision.pending(conn, tid)


@pytest.mark.parametrize("ask", ["agent_authored", "old_text", "other_stop"])
def test_remote_unblock_without_a_true_release_offer_is_a_plain_unblock(kanban_home, own_gateway, ask):
    """An ask an agent wrote, one filed before the stamp existed, and a stamp naming another stop: none offered
    this release, so the click is today's unblock (refused on a pending spend decision) and releases nothing."""
    from hermes_cli import kanban_card_answer as ca
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]

        def change(request):
            if ask == "agent_authored":
                request.pop("release_target")
                request["action"] = "Unblock = I archive the root card."
            elif ask == "old_text":
                request.pop("release_target")
                request["action"] = ("Decide how to handle: Bounded work. Unblock = ask the owner to record a "
                                     "bounded decision; the button cannot release this hold")
            else:
                request["release_target"]["episode"] = "an-earlier-stop"
        _restamp(conn, tid, change)
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held|spend_decision_pending"):
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and _no_release(conn, tid)


def test_remote_unblock_refused_by_containment_leaves_the_ask_and_records_the_reason(kanban_home, own_gateway,
                                                                                    monkeypatch):
    from hermes_cli import kanban_card_answer as ca
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        generation = ca.snapshot(conn, tid)["generation"]
        monkeypatch.setattr("hermes_cli.kanban_containment_guard.pending", lambda c, task, **kw: task == tid)
        with pytest.raises(ca.AnswerRefused, match="release_refused: containment_hold"):
            _retry(conn, tid, generation)
        assert _row(conn, tid)[0] == "triage" and _row(conn, tid)[2] and _no_release(conn, tid)
        notes = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND author='user-steps'", (tid,))]
        assert len(notes) == 1 and "containment_hold" in notes[0]


def _decision_card_with_ask(conn):
    """A plain budget stop no chat owns, its decision card, and the brief's ask filed on that card."""
    from hermes_cli import kanban_stranded_brief as brief
    from hermes_cli.kanban_ask_resolved import store_request
    root = kb.create_task(conn, title="Ownerless work", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (root,))
        kb._append_event(conn, root, decision.EVENT, {"reason": "Spend budget: 15 runs"})
    card = _legacy_wrapper(conn, root)  # historical release compatibility, not new supervisor generation
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (card,))
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone())
        ask = brief.request(conn, row, None, 3 * HOUR, "events: spend_budget_triaged runs=15")
        store_request(conn, card, ask)  # with the sweep's own record of it, as _escalate writes both
        kb._append_event(conn, card, stranded.EVENT, {"request": True, "release_target": ask["release_target"]})
    return root, card


def test_remote_unblock_on_a_decision_card_releases_its_stopped_card(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_spend_reviewer as reviewer
    monkeypatch.setattr(reviewer, "queue", lambda *a, **kw: False)
    with kbc.connect_closing() as conn:
        root, card = _decision_card_with_ask(conn)
        assert json.loads(_row(conn, card)[2])["release_target"]["task"] == root
        receipt = _answer(conn, card, "unblock")
        assert receipt["task"] == card and kb.get_task(conn, root).status == "ready"
        assert kb.get_task(conn, card).status == "archived" and not decision.pending(conn, root)


def test_remote_unblock_on_a_stale_decision_card_does_not_release_a_later_stop(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_spend_reviewer as reviewer
    monkeypatch.setattr(reviewer, "queue", lambda *a, **kw: False)
    with kbc.connect_closing() as conn:
        root, card = _decision_card_with_ask(conn)
        with kb.write_txn(conn):  # the card stopped again: the ask on the first stop's decision card is stale
            kb._append_event(conn, root, "promoted_manual", {"actor": "someone", "reason": "one more"})
            kb._append_event(conn, root, decision.EVENT, {"reason": "Spend budget: again"})
        assert decision.pending(conn, root)
        _answer(conn, card, "unblock")  # today's plain unblock of the decision card itself
        assert kb.get_task(conn, card).status != "blocked"
        assert kb.get_task(conn, root).status == "triage" and _no_release(conn, root)


def test_remote_unblock_on_a_stamp_the_sweep_did_not_record_is_a_plain_unblock(kanban_home, own_gateway):
    """The ask column alone is not proof: a stamped ask with no escalation record of this board releases nothing."""
    from hermes_cli import kanban_card_answer as ca
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        with kb.write_txn(conn):  # as if the row had arrived from another gateway without the home's event
            conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.release_target') "
                         "WHERE task_id=? AND kind=?", (tid, stranded.EVENT))
        assert json.loads(_row(conn, tid)[2])["release_target"]["task"] == tid
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held|spend_decision_pending"):
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and _no_release(conn, tid)


# --- dead release buttons: repaired only on this board's own filing record -------------------------------------

def _offer_ok(conn, tid):
    from hermes_cli import kanban_release
    return kanban_release.offered(conn, tid, json.loads(_row(conn, tid)[2])) == tid


def _refiles(conn, tid):
    return _events(conn, tid, stranded.REFILED)


def _forge(conn, tid, *, episode="made-up-stop", text="Unblock = I archive the root card."):
    """A column write from elsewhere: an ask with a release stamp this board never filed."""
    ask = {"action": text, "why_human": "Only you can decide.", "interaction": "approval",
           "scope": "Forged", "evidence": "none", "release_target": {"task": tid, "episode": episode}}
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(ask), tid))
    return ask


def _true_stamp(conn, tid):
    from hermes_cli import kanban_release
    return kanban_release.offer(conn, tid)["episode"]


def test_an_ask_filed_before_the_offer_was_recorded_is_repaired_once(kanban_home, own_gateway):
    """The #4245-era shape: this board's escalation event with ``request`` and no stamp."""
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        cfg = {"kanban": {"triage_owner_chat": DESK}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == [tid] and _offer_ok(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.release_target','$.ask_sha256') "
                         "WHERE task_id=? AND kind=?", (tid, stranded.EVENT))
            conn.execute("UPDATE tasks SET block_kind='transient' WHERE id=?", (tid,))
        assert not _offer_ok(conn, tid)
        assert _sweep(conn, now=_later(3.2), cfg=cfg) == [tid] and _offer_ok(conn, tid)
        ask = json.loads(_row(conn, tid)[2])
        (refiled,) = _refiles(conn, tid)
        assert refiled["release_target"] == ask["release_target"] and refiled["request"] is True
        assert _row(conn, tid)[1] == "needs_input"  # the block kind is fixed as _escalate fixes it
        # One escalation, so the desk chat is told once: the re-file is its own event kind.
        assert len(_events(conn, tid, stranded.EVENT)) == 1
        _, seen = notify.unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id="desk-chat",
                                               kinds=(stranded.EVENT,))
        assert [e.kind for e in seen] == [stranded.EVENT]
        assert _sweep(conn, now=_later(3.4), cfg=cfg) == [] and len(_refiles(conn, tid)) == 1
        _answer(conn, tid, "unblock")
        assert kb.get_task(conn, tid).status == "ready"


def test_a_rewritten_ask_column_is_refiled_at_most_once_per_episode(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        results = []
        for n, hours in enumerate((3.2, 3.4, 3.6)):  # rewritten before each of three sweeps
            _forge(conn, tid, episode=_true_stamp(conn, tid), text=f"Unblock = do something else ({n}).")
            assert not _offer_ok(conn, tid)  # the true stamp on text this board did not write is no offer
            results.append(_sweep(conn, now=_later(hours), cfg={}))
        assert results == [[tid], [], []] and len(_refiles(conn, tid)) == 1
        assert json.loads(_row(conn, tid)[2])["action"].startswith("Unblock = do something else (2)")
        assert not _offer_ok(conn, tid)
        with pytest.raises(Exception):  # the click on the last forgery is today's plain unblock: refused
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and _no_release(conn, tid)


def test_a_forged_stamp_on_a_young_card_gets_no_refile_and_no_offer(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)  # stopped moments ago: never escalated
        forged = _forge(conn, tid, episode=_true_stamp(conn, tid))
        assert _sweep(conn, now=int(time.time()) + 60, cfg={}) == []
        assert json.loads(_row(conn, tid)[2]) == forged and not _offer_ok(conn, tid)
        assert not _refiles(conn, tid) and not _events(conn, tid, stranded.EVENT)
        with pytest.raises(Exception):
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and _no_release(conn, tid)
        # Old enough and still unowned, it is escalated like any unowned stop: the board's own ask replaces the forgery.
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert json.loads(_row(conn, tid)[2])["action"].startswith("Decide how to handle: Bounded work")
        assert _offer_ok(conn, tid) and not _refiles(conn, tid) and len(_events(conn, tid, stranded.EVENT)) == 1


def test_a_forged_stamp_on_a_card_whose_decision_card_is_open_gets_no_refile(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_spend_reviewer as reviewer
    monkeypatch.setattr(reviewer, "queue", lambda *a, **kw: False)
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Ownerless work", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (root,))
            kb._append_event(conn, root, decision.EVENT, {"reason": "Spend budget: 15 runs"})
        _legacy_wrapper(conn, root)  # historical open decision card, not newly generated recovery work
        forged = _forge(conn, root, episode=_true_stamp(conn, root))
        for hours in (3, 3.2):
            assert root not in _sweep(conn, now=_later(hours), cfg={})
        assert json.loads(_row(conn, root)[2]) == forged and not _offer_ok(conn, root)
        assert not _refiles(conn, root) and not _events(conn, root, stranded.EVENT)


def test_an_escalation_whose_origin_was_overwritten_is_not_this_boards_evidence(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid] and _offer_ok(conn, tid)
        with kb.write_txn(conn):
            if "origin_gateway" not in {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}:
                conn.execute("ALTER TABLE task_events ADD COLUMN origin_gateway TEXT")
            conn.execute("UPDATE task_events SET origin_gateway='worker-9' WHERE task_id=? AND kind=?",
                         (tid, stranded.EVENT))
        assert not _offer_ok(conn, tid)
        assert _sweep(conn, now=_later(3.2), cfg={}) == [] and not _refiles(conn, tid) and not _offer_ok(conn, tid)


def test_a_card_that_stopped_again_under_its_old_ask_is_escalated_for_the_new_stop(kanban_home, own_gateway):
    """No filing record exists for the new park episode, so nothing is re-filed: the stale ask is no open ask
    and the card passes the normal gates like any unowned stop."""
    from hermes_cli import kanban_release
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        old = json.loads(_row(conn, tid)[2])["release_target"]
        with kb.write_txn(conn):  # decided elsewhere and stopped again; the old ask is still on the row
            kb._append_event(conn, tid, "promoted_manual", {"actor": "someone", "reason": "one more"})
            kb._append_event(conn, tid, decision.EVENT, {"reason": "Spend budget: again"})
        assert decision.pending(conn, tid) and not _offer_ok(conn, tid)
        assert _sweep(conn, now=int(time.time()) + 60, cfg={}) == []  # too young: nothing written yet
        assert json.loads(_row(conn, tid)[2])["release_target"] == old
        assert _sweep(conn, now=_later(3.2), cfg={}) == [tid]
        new = json.loads(_row(conn, tid)[2])["release_target"]
        assert new == kanban_release.offer(conn, tid) and new["episode"] != old["episode"] and _offer_ok(conn, tid)
        assert not _refiles(conn, tid) and len(_events(conn, tid, stranded.EVENT)) == 2
        _answer(conn, tid, "unblock")
        assert kb.get_task(conn, tid).status == "ready"


def test_an_ask_that_can_offer_nothing_now_is_not_refiled(kanban_home, own_gateway):
    """A safety-restricted card offers no release: its stamped ask is left as it is (the click records the refusal)."""
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        stop = decision.pending_episode(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted",
                             {"cause": "safety_refusal", "episode": stop.get("uid") or str(stop["id"])})
            conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.release_target') "
                         "WHERE task_id=? AND kind=?", (tid, stranded.EVENT))
        before = _row(conn, tid)[2]
        assert _sweep(conn, now=_later(3.2), cfg={}) == [] and _row(conn, tid)[2] == before
