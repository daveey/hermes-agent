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
        assert request["interaction"] == "approval" and request["scope"] == "This card only"
        assert request["action"].startswith("Decide this parked card (Bounded work): release it, archive it, or re-sc")
        assert f"Unblock = run `hermes kanban promote {tid} --reason" in request["action"]
        assert request["why_human"].startswith("Parked 3h with no live owner and no request (spend_budget_triaged")
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
        (notice,) = _events(conn, tid, decision.NOTICE)
        sup = notice["producer"]["task_id"]
        assert stranded.open_decision_card(conn, tid) == sup
        assert _sweep(conn, now=_later(3), cfg={}) == []
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (sup,))
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert card_is_open_human_ask(*_row(conn, tid))


def test_transient_triage_card_is_rekinded(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn, kind="transient")
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        status, kind, raw = _row(conn, tid)
        assert kind == "needs_input"
        assert card_is_open_human_ask(status, kind, raw)
        assert "agent_block_escalated: retry needed" in json.loads(raw)["why_human"]


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
        assert "blocked: parked again" in json.loads(_row(conn, tid)[2])["why_human"]


def test_thresholds_come_from_config(kanban_home, own_gateway):
    with kbc.connect_closing() as conn:
        tid = _parked(conn)
        cfg = {"kanban": {"stranded_park_seconds": 4 * HOUR}}
        assert _sweep(conn, now=_later(3), cfg=cfg) == []
        assert _sweep(conn, now=_later(5), cfg=cfg) == [tid]
    assert stranded.settings({}) == (stranded.DEFAULT_PARK_SECONDS, stranded.DEFAULT_OWNER_IDLE_SECONDS, "",
                                     stranded.DEFAULT_SWEEP_INTERVAL_SECONDS)


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


def test_goal_hold_owns_the_park(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_goal_recovery as goal
    with kbc.connect_closing() as conn:
        tid = _parked(conn, status="blocked", event="blocked", kind="needs_input")
        monkeypatch.setattr(goal, "current_park", lambda c, task_id: task_id == tid)
        assert _sweep(conn, now=_later(3), cfg={}) == []


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


def test_spend_pending_unblock_answer_routes_to_promote(kanban_home, own_gateway):
    """The Unblock button cannot release a pending spend decision (kanban_card_answer refuses it), so the ask names
    the promote route; archive still works from the button, and promote releases the card."""
    from hermes_cli import kanban_card_answer as ca
    with kbc.connect_closing() as conn:
        tid = _spend_stopped_on_dead_chat(conn, kanban_home)
        assert _sweep(conn, now=_later(3), cfg={}) == [tid]
        assert f"hermes kanban promote {tid} --reason" in json.loads(_row(conn, tid)[2])["action"]
        # The fence's spend_decision_pending refusal surfaces from apply() as ask_changed_or_held.
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held|spend_decision_pending"):
            _answer(conn, tid, "unblock")
        assert _row(conn, tid)[0] == "triage" and _row(conn, tid)[2]
        assert kb.promote_task(conn, tid, actor="operator", reason="rescoped")[0]
        assert kb.get_task(conn, tid).status == "ready"
