"""Only an ordinary budget stop gets a decision card; a stop with a cause is left to the stranded park. [daveey]

A "Spend budget decision" card can do one thing: promote its source (``spend_promote``). That releases a plain
run/token budget stop and nothing else. For a ``capability_unavailable``, ``scope_stopped`` or ``safety_refusal``
stop the card's worker could only retain the card, which is another stop and no decision (32 of them on
2026-11-23). With no live owning chat such a stop now records the producer ``{"kind": "unavailable"}`` and the
stranded-park escalation is its owner.
"""
from __future__ import annotations

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_containment_actor as support
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_budget_hold as hold_helpers
from tests.hermes_cli import test_kanban_spend_decision_routing as routing
from tests.hermes_cli.test_kanban_spend_route_repair import _route, _stop

kanban_home = helpers.kanban_home
board = support.board
own_gateway = routing.own_gateway

CAUSES = ["capability_unavailable", "scope_stopped", "safety_refusal"]


def _cards(conn):
    return [r[0] for r in conn.execute("SELECT id FROM tasks WHERE created_by='kanban-spend-budget'")]


def _notices(conn, tid):
    return helpers._events(conn, tid, decision.NOTICE)


@pytest.mark.parametrize("reason", ["capability_unavailable", "scope_stopped"])
def test_retain_with_no_live_chat_is_unavailable_and_files_no_card(board, monkeypatch, reason):
    """The worker's own ``kanban_retain`` is the entry path: its stop carries the cause and no card is made."""
    from hermes_cli import kanban_retain as retain
    tid, _, session = support.bind(board, monkeypatch)
    with scoped_current_session_id(session):
        result = retain.retain(board, board="default", summary="Cannot finish from here", reason=reason,
                               next_step="Owner supplies the missing prerequisite",
                               release_condition="Prerequisite exists", evidence=["fixture"])
    (event,) = _notices(board, tid)
    assert event.payload["producer"] == {"kind": "unavailable"}
    assert result["next_action"]["owner"] == {"kind": "unavailable"}
    assert _cards(board) == []
    assert kb.get_task(board, tid).status == "triage" and decision.pending(board, tid)


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
