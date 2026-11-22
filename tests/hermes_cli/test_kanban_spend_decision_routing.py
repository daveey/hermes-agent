"""Where a spend stop's decision notice goes, and what keeps it honest downstream. [daveey]

#3123's own suite left five changed lines unpinned (runtime-15 audit t_01a1dd14ce5c59e4): the open-run guard of
the pending hold, the headless and local branches of the producer's chat-link binding, the wake fold's premise for
the decision notice, and the TUI poller's admission of its kind. Each test here fails when its line is mutated.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_budget_hold as hold_helpers

kanban_home = helpers.kanban_home
stopped = hold_helpers.stopped

OWN = "own-gw"


def _links(conn, tid, *rows):
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS task_chat_links (task_id TEXT, chat_gateway TEXT, "
                     "chat_profile TEXT, chat_session TEXT, released INTEGER, created_at INTEGER)")
        for gw, profile, session in rows:
            conn.execute("INSERT INTO task_chat_links VALUES (?, ?, ?, ?, 0, 0)", (tid, gw, profile, session))


def _subs(conn, tid):
    return sorted((r[0], r[1]) for r in conn.execute(
        "SELECT platform, chat_id FROM kanban_notify_subs WHERE task_id=?", (tid,)))


@pytest.fixture
def own_gateway(monkeypatch):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (OWN, {OWN, "remote"}))


def test_headless_chat_link_is_not_bound_and_a_supervisor_decides(kanban_home, own_gateway):
    """A worker transcript's chat link is no owner: binding it would send the decision to a machine session."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        _links(conn, tid, ("remote", "default", "local-" + "b" * 64))
        assert budget.hold(conn, tid) == "triage"
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        producer = event.payload["producer"]
        assert producer["kind"] == "supervisor"
        assert kb.get_task(conn, producer["task_id"]).assignee == "default"
        assert _subs(conn, tid) == []


@pytest.mark.parametrize(("gw", "profile", "expected"), [
    (OWN, "default", ("tui", "human-chat")),
    (OWN, "other", ("hub", f"{OWN}/other/human-chat")),
    ("remote", "default", ("hub", "remote/default/human-chat")),
], ids=["own-gateway-active-profile", "own-gateway-other-profile", "remote-gateway"])
def test_chat_link_binding_platform(kanban_home, own_gateway, gw, profile, expected):
    """This gateway's own chat under the active profile is a local tui session; any other is a hub route."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        _links(conn, tid, (gw, profile, "human-chat"))
        assert budget.hold(conn, tid) == "triage"
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"] == {"kind": "chats"}
        assert _subs(conn, tid) == [expected]
        _, events = notify.unseen_events_for_sub(conn, task_id=tid, platform=expected[0], chat_id=expected[1],
                                                 kinds=(decision.NOTICE,))
        assert [e.kind for e in events] == [decision.NOTICE]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["ready", "review"])
def test_pending_hold_leaves_a_card_with_an_open_run_untouched(kanban_home, status):
    """An unrecorded release whose run is still open (expired claim) is not pulled into triage under that run."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        assert budget.hold(conn, tid) == "triage"
        run_id = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, current_run_id=?, claim_lock='gone-host:1', "
                         "claim_expires=1 WHERE id=?", (status, run_id, tid))
        assert decision.pending(conn, tid)
        budget.hold(conn, tid)
        task = kb.get_task(conn, tid)
        assert (task.status, task.current_run_id) == (status, run_id)
        # Control: once the run is closed the same pending hold does take the card back to triage.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,))
        assert budget.hold(conn, tid) == "triage"
        assert kb.get_task(conn, tid).status == "triage"


def _decision_card(tid):
    return {"task_id": tid, "board": "", "kinds": [decision.NOTICE],
            "text": f"[kanban] Task {tid} spend_budget_decision_ready.\nHeld in triage until a recorded decision."}


def test_stale_decision_wake_is_relabelled_after_promote(kanban_home):
    """A decision wake that waited while someone promoted the card must not ask the chat to decide again."""
    from gateway.kanban_wake_fold import refresh_card
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        assert budget.hold(conn, tid) == "triage"
        card = _decision_card(tid)
        assert refresh_card(card) == card  # still in triage: the premise holds, delivered as is
        assert kb.promote_task(conn, tid, actor="operator", reason="rescoped")[0]
        assert kb.get_task(conn, tid).status == "ready"
    fresh = refresh_card(card)
    assert fresh["text"].startswith(f"[kanban] Task {tid} ready.\n")
    assert 'said "spend_budget_decision_ready"; that no longer holds. Live status: ready.' in fresh["text"]
    assert fresh["kinds"] == ["ready"]


def test_tui_poller_delivers_the_decision_notice(kanban_home):
    """A Desktop/TUI chat owning the stop gets the decision notice from its own poller, once."""
    from tui_gateway import server
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="desk-spend")
        assert budget.hold(conn, tid) == "triage"
    texts = [t for t in server._collect_kanban_notifications({"session_key": "desk-spend"})
             if "recorded decision" in t]
    assert len(texts) == 1 and tid in texts[0]
    assert "promote" in texts[0] and "archive" in texts[0]
    assert [t for t in server._collect_kanban_notifications({"session_key": "desk-spend"})
            if "recorded decision" in t] == []


def _state_session(home, sid, *, ended_ago=None, **flags):
    from tests.hermes_cli.test_kanban_stranded_park import _session
    _session(home, sid, ended_ago=ended_ago, active_ago=ended_ago, **flags)


@pytest.mark.parametrize("dead", ["ended-idle", "archived", "missing"])
def test_dead_local_chat_is_no_route_and_a_supervisor_decides(kanban_home, own_gateway, dead):
    """A chat of this gateway that is ended and idle past kanban.stranded_owner_idle_seconds, archived, or absent
    from a readable state.db is not bound or counted: the decision goes to a supervisor card (kanban_stranded_park)."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        if dead == "missing":
            _state_session(kanban_home, "someone-else")
        else:
            _state_session(kanban_home, "human-chat", **({"ended_ago": 2 * 86400} if dead == "ended-idle"
                                                         else {"archived": 1}))
        _links(conn, tid, (OWN, "default", "human-chat"))
        notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="human-chat")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='human-chat' WHERE id=?", (tid,))
        assert budget.hold(conn, tid) == "triage"
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        producer = event.payload["producer"]
        assert producer["kind"] == "supervisor"
        assert kb.get_task(conn, producer["task_id"]).status not in ("done", "archived")


@pytest.mark.parametrize("ended_ago", [None, 3600], ids=["open", "ended-recently"])
def test_live_local_chat_in_state_db_is_the_route(kanban_home, own_gateway, ended_ago):
    """An open chat, or one closed recently (a CLI chat closed with cli_close resumes in Desktop), still owns it."""
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        _state_session(kanban_home, "human-chat", ended_ago=ended_ago)
        _links(conn, tid, (OWN, "default", "human-chat"))
        assert budget.hold(conn, tid) == "triage"
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"] == {"kind": "chats"}
        assert _subs(conn, tid) == [("tui", "human-chat")]
