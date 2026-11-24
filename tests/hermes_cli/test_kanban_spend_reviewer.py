"""The immediate spend-stop reviewer (``kanban_spend_reviewer``): a stopped card is judged at once instead of
waiting for an idle chat or a queued decision card, and only verifiable progress buys more runs."""
from __future__ import annotations

import time
import types

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as ksb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home
PROGRESS = "Landed PR 4221 with request-stage tracing; the hub sample starts once it deploys"


class _Inline:
    """Run the review thread synchronously so the test sees its result."""

    def __init__(self, target, args, name=None, daemon=None):
        self._call = lambda: target(*args)

    def start(self):
        self._call()


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(Thread=_Inline))


def _stopped(conn, *, summary: str = PROGRESS) -> tuple[str, str]:
    tid = kb.create_task(conn, title="Transport stall", assignee="default")
    helpers._runs(conn, tid, ksb.DEFAULT_RUNS)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET summary = ? WHERE task_id = ? AND id = (SELECT MAX(id) FROM task_runs "
                     "WHERE task_id = ?)", (summary, tid, tid))
    assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
    return tid, reviewer.episode_of(decision.pending_episode(conn, tid))


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def _grant(runs=5, quote=PROGRESS[:60]):
    return {"decision": "grant", "runs": runs, "progress_quote": quote, "reason": "PR landed; sample is next."}


def test_a_stop_is_queued_for_review_instead_of_waking_a_chat_or_opening_a_decision_card(kanban_home, spend_reviewer):
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        assert reviewer.state(conn, tid, episode) == reviewer.QUEUED
        assert decision.NOTICE not in _kinds(conn, tid)
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by = 'kanban-spend-budget'").fetchone()[0] == 0
        decision.safe_ensure_producer(conn, tid)  # the route sweep leaves a queued review alone
        assert decision.NOTICE not in _kinds(conn, tid)


def test_a_grant_with_quoted_progress_promotes_into_a_fresh_window_of_exactly_those_runs(
        kanban_home, spend_reviewer, inline):
    spend_reviewer.append(_grant(runs=5))
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        assert reviewer.start_due(conn) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        assert reviewer.state(conn, tid, episode) == reviewer.GRANTED
        assert ksb.card_limits(conn, tid, helpers.LIMITS).runs == 5
        assert ksb.spend(conn, tid).runs == 0 and not decision.pending(conn, tid)
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) is None
        assert "Spend reviewer granted 5" in kb.list_comments(conn, tid)[-1].body


@pytest.mark.parametrize("verdict", [
    _grant(quote="this sentence never appeared anywhere in the card's window"),  # invented progress
    _grant(runs=ksb.DEFAULT_RUNS + 1),  # more than the allowance that just ran out
    {"decision": "escalate", "reason": "Runs repeat the same wait with no new evidence."},
    None,  # judge unavailable
])
def test_anything_but_a_checked_grant_escalates_and_routes_the_stop_as_before(
        kanban_home, spend_reviewer, inline, verdict):
    if verdict is not None:
        spend_reviewer.append(verdict)
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage" and decision.pending(conn, tid)
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert decision.NOTICE in _kinds(conn, tid)  # the usual owner now decides
        assert "Spend reviewer did not grant" in kb.list_comments(conn, tid)[-1].body


def test_a_human_comment_after_the_stop_is_heard_before_any_grant(kanban_home, spend_reviewer, inline):
    spend_reviewer.append(_grant())
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        kb.add_comment(conn, tid, "david", "Hold this one, I want to look first.", writer_kind="human")
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED


def test_grants_are_capped_until_someone_else_decides(kanban_home, spend_reviewer, inline):
    with kbc.connect_closing() as conn:
        tid, _ = _stopped(conn)
        clock = int(time.time())
        for _ in range(reviewer.DEFAULT_MAX_GRANTS):
            spend_reviewer.append(_grant(runs=2))
            reviewer.start_due(conn, now=clock)
            assert kb.get_task(conn, tid).status == "ready"
            with kb.write_txn(conn):  # the two granted runs, after the promotion that opened the window
                for _ in range(2):
                    clock += 100
                    conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                                 "summary) VALUES (?, 'default', 'done', ?, ?, 'continued', ?)",
                                 (tid, clock - 90, clock, PROGRESS))
            clock += 10
            assert ksb.hold(conn, tid, now=clock, lim=helpers.LIMITS) == "triage"
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        assert reviewer.state(conn, tid, episode) is None  # not queued: the owner decides
        assert decision.NOTICE in _kinds(conn, tid)


def test_an_undecided_routed_stop_is_reviewed_and_its_decision_card_closed(kanban_home, spend_reviewer, inline,
                                                                            monkeypatch):
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        route = decision.episode_route(conn, tid, episode)
        assert route["kind"] == "supervisor"
        monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 2))
        assert reviewer.start_due(conn) == []  # not yet stale
        spend_reviewer.append(_grant(runs=3))
        later = int(time.time()) + reviewer.BACKLOG_AFTER + 5
        assert reviewer.start_due(conn, now=later) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, route["task_id"]).status == "archived"


def test_an_unfinished_review_is_retried_then_escalated(kanban_home, spend_reviewer, monkeypatch):
    started = []
    monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(
        Thread=lambda target, args, **kw: types.SimpleNamespace(start=lambda: started.append(args))))
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        now = int(time.time())
        for attempt in range(reviewer.MAX_ATTEMPTS):
            reviewer._INFLIGHT.clear()  # the abandoned thread's slot
            assert reviewer.start_due(conn, now=now + attempt * (reviewer.LEASE_SECONDS + 1)) == [tid]
        reviewer._INFLIGHT.clear()
        assert reviewer.start_due(conn, now=now + reviewer.MAX_ATTEMPTS * (reviewer.LEASE_SECONDS + 1)) == []
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert decision.NOTICE in _kinds(conn, tid)


def test_scope_stops_are_never_reviewed(kanban_home, spend_reviewer, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Scoped", assignee="default")
        helpers._runs(conn, tid, ksb.DEFAULT_RUNS)
        monkeypatch.setattr(decision, "execution_restricted", lambda c, t: True)
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        assert reviewer.state(conn, tid, episode) is None


def test_the_verdict_parser_takes_json_only():
    assert reviewer.parse('Sure.\n{"decision": "grant", "runs": 2}') == {"decision": "grant", "runs": 2}
    assert reviewer.parse('{"decision": "maybe"}') is None
    assert reviewer.parse("no json") is None
