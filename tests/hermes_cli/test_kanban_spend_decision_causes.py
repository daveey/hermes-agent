"""Only an ordinary budget stop gets a decision card; a stop with a cause is left to the stranded park. [daveey]

A "Spend budget decision" card's worker decides spend. It cannot restore a capability, renew a scope or clear a
provider refusal, so for a ``capability_unavailable``, ``scope_stopped`` or ``safety_refusal`` stop it could only
retain the card, which is another stop and no decision (32 of them on 2026-11-23). With no live owning chat such
a stop now records the producer ``{"kind": "unavailable"}``. From there: a chat of the card that is live again
takes the decision, else the stranded-park sweep asks a human, unless an open parent explains the wait.
"""
from __future__ import annotations

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_stranded_park as stranded
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_open_asks import card_is_open_human_ask
from tests.hermes_cli import test_kanban_containment_actor as support
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_budget_hold as hold_helpers
from tests.hermes_cli import test_kanban_spend_decision_routing as routing
from tests.hermes_cli import test_kanban_stranded_park as park_tests
from tests.hermes_cli.test_kanban_spend_route_repair import _route, _stop

kanban_home = helpers.kanban_home
board = support.board
own_gateway = routing.own_gateway
desk_gateway = park_tests.own_gateway

CAUSES = ["capability_unavailable", "scope_stopped", "safety_refusal"]
RETAIN = dict(summary="Cannot finish from here", next_step="Owner supplies the missing prerequisite",
              release_condition="Prerequisite exists", evidence=["fixture"])


@pytest.fixture(autouse=True)
def _fresh_recheck_pacing():
    decision._RECHECKED.clear()


def _sweep(conn, hours):
    """One working stranded-park pass ``hours`` from now (the per-board throttle is cleared first)."""
    import time
    stranded._LAST_RUN.clear()
    return stranded.sweep_stranded_parks(conn, now=int(time.time()) + int(hours * 3600), cfg={"kanban": {}})


def _asked(conn, tid):
    row = conn.execute("SELECT status, block_kind, human_request FROM tasks WHERE id=?", (tid,)).fetchone()
    return card_is_open_human_ask(row[0], row[1], row[2])


def _retain(conn, monkeypatch, reason):
    from hermes_cli import kanban_retain as retain
    tid, _, session = support.bind(conn, monkeypatch)
    with scoped_current_session_id(session):
        return tid, retain.retain(conn, board="default", reason=reason, **RETAIN)


def _cards(conn):
    return [r[0] for r in conn.execute("SELECT id FROM tasks WHERE created_by='kanban-spend-budget'")]


def _notices(conn, tid):
    return helpers._events(conn, tid, decision.NOTICE)


@pytest.mark.parametrize("reason", ["capability_unavailable", "scope_stopped"])
def test_retain_with_no_live_chat_is_unavailable_and_files_no_card(board, monkeypatch, reason):
    """The worker's own ``kanban_retain`` is the entry path: its stop carries the cause and no card is made."""
    tid, result = _retain(board, monkeypatch, reason)
    (event,) = _notices(board, tid)
    assert event.payload["producer"] == {"kind": "unavailable"}
    assert result["next_action"]["owner"] == {"kind": "unavailable"}
    # The receipt does not claim a pending escalation to a route nobody is on.
    escalation = result["next_action"]["escalation"]
    assert (escalation["route"], escalation["state"]) == (None, "unavailable")
    assert _cards(board) == []
    assert kb.get_task(board, tid).status == "triage" and decision.pending(board, tid)
    # Handoff: nobody was routed, so the stranded-park sweep asks a human once the park is old enough.
    assert _sweep(board, 1) == [] and not _asked(board, tid)
    assert _sweep(board, 3) == [tid] and _asked(board, tid)
    assert _cards(board) == [] and decision.pending(board, tid)


def test_retain_receipt_still_names_a_live_decision_owner(board, monkeypatch):
    """Control for the escalation state: a routed stop keeps ``pending`` and its route."""
    tid, _, session = support.bind(board, monkeypatch)
    with kb.write_txn(board):
        kb._append_event(board, tid, decision.EVENT, {"reason": "budget stop"})
    decision.ensure_producer(board, tid)
    route = _route(board, tid)
    assert route["kind"] == "supervisor"
    from hermes_cli import kanban_retain as retain
    with scoped_current_session_id(session):
        result = retain.retain(board, board="default", reason="decision_pending", **RETAIN)
    escalation = result["next_action"]["escalation"]
    assert (escalation["route"], escalation["state"]) == (route, "pending")


def test_retained_card_with_an_open_parent_is_asked_about_only_after_the_parent_closes(board, monkeypatch):
    """Intended gap: an explained wait is not an ask (kanban_retained_wait). No card, no route, no request while
    the parent is open; the sweep asks once it has closed."""
    tid, _ = _retain(board, monkeypatch, "capability_unavailable")
    parent = kb.create_task(board, title="Restores the capability", assignee="default")
    with kb.write_txn(board):
        board.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
    assert _route(board, tid) == {"kind": "unavailable"} and _cards(board) == []
    assert _sweep(board, 3) == [] and not _asked(board, tid)
    assert _sweep(board, 30) == [] and not _asked(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    assert _sweep(board, 30) == [tid] and _asked(board, tid)


@pytest.mark.parametrize("cause", CAUSES)
def test_stop_with_a_cause_and_no_live_chat_is_unavailable_and_files_no_card(kanban_home, cause):
    """The dispatcher's producer repair is the other entry path (safety stops only arrive this way)."""
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid) == {"kind": "unavailable"}
        assert _cards(conn) == []
        # The route is terminal for the episode: later sweeps neither re-notify nor file a card.
        decision.ensure_producer(conn, tid)
        decision.repair_triage_routes(conn, skip=set())
        assert len(_notices(conn, tid)) == 1 and _cards(conn) == []
        assert kb.get_task(conn, tid).status == "triage" and decision.pending(conn, tid)
        # Handoff: the stranded-park sweep asks a human after kanban.stranded_park_seconds (default 2 h).
        assert "owner_missing" in describe(conn, tid)["violations"]
        assert _sweep(conn, 1) == [] and not _asked(conn, tid)
        assert _sweep(conn, 3) == [tid] and _asked(conn, tid)
        assert stranded.EVENT in [e.kind for e in kb.list_events(conn, tid)]
        assert _sweep(conn, 4) == []  # once per park episode
        # ``hermes kanban stranded`` reads the stored ask as the owner.
        assert "owner_missing" not in describe(conn, tid)["violations"]
        assert _cards(conn) == [] and decision.pending(conn, tid)
        # Only while the card is still parked: forced out of triage, the stored request owns nothing.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        assert "owner_missing" in describe(conn, tid)["violations"]


@pytest.mark.parametrize("desk", [f"{park_tests.OWN}/default/desk-chat", "remote/default/desk"], ids=["local", "remote"])
@pytest.mark.parametrize("cause", CAUSES)
def test_the_desk_chat_the_sweep_binds_is_an_observer_not_a_returned_owner(kanban_home, desk_gateway, monkeypatch,
                                                                           cause, desk):
    """The sweep's ``shepherd`` link and subscription for kanban.triage_owner_chat are live but own nothing:
    the recheck must not take the write lock for them, ever."""
    import time
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        decision.ensure_producer(conn, tid)
        park_tests._session(kanban_home, "desk-chat")
        stranded._LAST_RUN.clear()
        assert stranded.sweep_stranded_parks(conn, now=int(time.time()) + 3 * 3600,
                                             cfg={"kanban": {"triage_owner_chat": desk}}) == [tid]
        reasons = [r[0] for r in conn.execute("SELECT json_extract(delivery_metadata,'$.reason') FROM "
                                              "kanban_notify_subs WHERE task_id=?", (tid,))]
        assert reasons == ["shepherd"]
        assert conn.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id=? AND kind='shepherd' "
                            "AND released=0", (tid,)).fetchone()[0] == 1

        def forbidden(*args, **kwargs):
            pytest.fail("an observer chat must not make the route recheck take the write lock")
        monkeypatch.setattr(kb, "write_txn", forbidden)
        for _ in range(4):
            decision._RECHECKED.clear()
            assert decision._producer_repair(conn, tid) is None
            decision.repair_triage_routes(conn, skip=set())
            decision.ensure_producer(conn, tid)
        assert [n.payload["producer"] for n in _notices(conn, tid)] == [{"kind": "unavailable"}]


@pytest.mark.parametrize("cause", CAUSES)
def test_a_chat_that_is_live_again_takes_the_ownerless_stop_once(kanban_home, own_gateway, cause):
    """Dead at the stop, live later: stranded-park counts the live link as an owner and stays quiet, so the
    route repair must send that chat the notice."""
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        routing._state_session(kanban_home, "human-chat", archived=1)
        routing._links(conn, tid, (routing.OWN, "default", "human-chat"))
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid) == {"kind": "unavailable"} and routing._subs(conn, tid) == []
        decision.repair_triage_routes(conn, skip=set())  # still dead: nothing new
        assert len(_notices(conn, tid)) == 1
        routing._state_session(kanban_home, "human-chat", archived=0)
        decision.repair_triage_routes(conn, skip=set())  # paced: this card was checked a moment ago
        assert len(_notices(conn, tid)) == 1
        decision._RECHECKED.clear()
        decision.repair_triage_routes(conn, skip=set())
        assert [n.payload["producer"] for n in _notices(conn, tid)] == [{"kind": "unavailable"}, {"kind": "chats"}]
        assert routing._subs(conn, tid) == [("tui", "human-chat")]
        for _ in range(2):
            decision._RECHECKED.clear()
            decision.repair_triage_routes(conn, skip=set())
            decision.ensure_producer(conn, tid)
        assert len(_notices(conn, tid)) == 2 and _cards(conn) == []


def test_capability_stop_is_not_sent_to_the_spend_reviewer(kanban_home, spend_reviewer):
    """More runs meet the same wall: the reviewer must not grant them. Control: a plain stop is queued."""
    from hermes_cli import kanban_spend_reviewer as reviewer
    with kbc.connect_closing() as conn:
        tid, plain = _stop(conn, "capability_unavailable"), _stop(conn)
        for card in (tid, plain):
            decision.ensure_producer(conn, card)
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        assert reviewer.state(conn, tid, episode) is None
        assert _route(conn, tid) == {"kind": "unavailable"}
        assert reviewer.state(conn, plain, reviewer.episode_of(decision.pending_episode(conn, plain))) == reviewer.QUEUED
        assert _notices(conn, plain) == [] and _cards(conn) == []


def test_restriction_recorded_apart_from_the_stop_files_no_card(kanban_home):
    """A budget stop restricted by a later ``decision_hold_restricted`` has no cause on the stop event itself."""
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        episode = decision.pending_episode(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted",
                             {"cause": "scope_stopped", "episode": episode.get("uid") or str(episode["id"])})
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid) == {"kind": "unavailable"}
        assert _cards(conn) == []


def test_plain_budget_stop_with_no_live_chat_still_gets_a_decision_card(kanban_home):
    with kbc.connect_closing() as conn:
        tid = hold_helpers.stopped(conn)
        assert budget.hold(conn, tid) == "triage"
        (event,) = _notices(conn, tid)
        producer = event.payload["producer"]
        assert producer["kind"] == "supervisor" and _cards(conn) == [producer["task_id"]]
        card = kb.get_task(conn, producer["task_id"])
        assert card.status not in ("done", "archived")
        assert f"hermes kanban promote {tid} --reason" in card.body and "or archive it" in card.body
        assert "split or re-scope" in card.body
        assert "retain this decision card" not in card.body and "non-executing owner" not in card.body
        assert "authority unavailable" not in card.body


@pytest.mark.parametrize("cause", [None, *CAUSES])
def test_live_owning_chat_is_still_the_route(kanban_home, own_gateway, cause):
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        routing._links(conn, tid, ("remote", "default", "human-chat"))
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid) == {"kind": "chats"}
        assert routing._subs(conn, tid) == [("hub", "remote/default/human-chat")]
        assert _cards(conn) == []
