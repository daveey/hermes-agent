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

    def is_alive(self):
        return False


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
        granted = ksb.card_override(conn, tid)  # the reviewer is an automated model review: an agent's limit
        assert granted["writer_kind"] == "agent" and "cache_read_writer_kind" not in granted
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
        Thread=lambda target, args, **kw: types.SimpleNamespace(start=lambda: started.append(args), is_alive=lambda: True)))
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


# --- paths the first review named ---------------------------------------------------------------------------------

def _routed_with_reviewer_off(conn, monkeypatch):
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    tid, episode = _stopped(conn)
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 2))
    return tid, episode


def test_a_stop_a_closed_decision_card_disposed_of_is_never_reviewed(kanban_home, spend_reviewer, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, episode = _routed_with_reviewer_off(conn, monkeypatch)
        card = decision.episode_route(conn, tid, episode)["task_id"]
        kb.archive_task(conn, card)
        assert decision.episode_route(conn, tid, episode)["kind"] == "dismissed"
        spend_reviewer.append(_grant())
        assert reviewer.start_due(conn, now=int(time.time()) + reviewer.BACKLOG_AFTER + 5) == []
        assert reviewer.state(conn, tid, episode) is None and kb.get_task(conn, tid).status == "triage"


def test_turning_the_reviewer_off_releases_a_queued_stop_to_the_usual_route(kanban_home, spend_reviewer, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        assert reviewer.state(conn, tid, episode) == reviewer.QUEUED
        monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
        decision.repair_triage_routes(conn, skip=set())
        assert decision.NOTICE in _kinds(conn, tid)


def test_an_ineligible_stalled_stop_never_takes_the_write_lock(kanban_home, spend_reviewer, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, _ = _routed_with_reviewer_off(conn, monkeypatch)
        monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 0))  # cap 0: never eligible
        txns = []
        real = kb.write_txn
        monkeypatch.setattr(kb, "write_txn", lambda c, **kw: (txns.append(1), real(c, **kw))[1])
        for tick in range(3):
            reviewer.start_due(conn, now=int(time.time()) + reviewer.BACKLOG_AFTER + 5 + tick)
        assert txns == [] and kb.get_task(conn, tid).status == "triage"


@pytest.mark.parametrize("route, ok", [
    ({"provider": reviewer.PROVIDER, "model": reviewer.MODEL, "billing_group": "subscription"}, True),
    ({"provider": reviewer.PROVIDER, "model": reviewer.MODEL}, False),  # no billing evidence
    ({"provider": "openrouter", "model": reviewer.MODEL, "billing_group": "subscription"}, False),
    ({"provider": reviewer.PROVIDER, "model": reviewer.MODEL, "billing_group": "metered"}, False),
])
def test_the_judge_answers_only_through_the_subscription_route(monkeypatch, route, ok):
    from agent import auxiliary_client as aux

    def call_llm(**kw):
        kw["route_info"].update(route)
        message = types.SimpleNamespace(content='{"decision": "escalate", "reason": "same wait"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(aux, "call_llm", call_llm)
    verdict = reviewer.judge({"_cap": 3, "_corpus": "", "title": "t"})
    assert (verdict == {"decision": "escalate", "reason": "same wait"}) is ok


def test_a_hung_judge_is_abandoned_at_the_deadline(monkeypatch):
    from agent import auxiliary_client as aux
    import threading
    release = threading.Event()
    monkeypatch.setattr(reviewer, "TIMEOUT", 1)
    monkeypatch.setattr(aux, "call_llm", lambda **kw: release.wait(30))
    try:
        assert reviewer.judge({"_cap": 3, "_corpus": "", "title": "t"}) is None
    finally:
        release.set()


def test_safety_stops_are_never_reviewed(kanban_home, spend_reviewer, monkeypatch):
    from hermes_cli import kanban_refusal_receipt
    monkeypatch.setattr(kanban_refusal_receipt, "restricted", lambda c, t: True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Refused", assignee="default")
        helpers._runs(conn, tid, ksb.DEFAULT_RUNS)
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        assert reviewer.state(conn, tid, episode) is None


def test_a_worker_copy_is_left_to_its_home_board(kanban_home, spend_reviewer, monkeypatch):
    from hermes_cli import kanban_wake_check
    with kbc.connect_closing() as conn:
        tid, episode = _routed_with_reviewer_off(conn, monkeypatch)
        monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda c, t: True)
        monkeypatch.setattr(kanban_wake_check, "foreign_ids", lambda c, own=None: {tid})
        spend_reviewer.append(_grant())
        assert reviewer.start_due(conn, now=int(time.time()) + reviewer.BACKLOG_AFTER + 5) == []
        assert reviewer.state(conn, tid, episode) is None


def test_an_open_parent_turns_a_grant_into_an_escalation(kanban_home, spend_reviewer, inline):
    spend_reviewer.append(_grant())
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        parent = kb.create_task(conn, title="Prerequisite", assignee="default")
        kb.link_tasks(conn, parent, tid)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert "open parents" in kb.list_comments(conn, tid)[-1].body


def test_reviews_per_process_are_bounded(kanban_home, spend_reviewer, monkeypatch):
    monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(
        Thread=lambda target, args, **kw: types.SimpleNamespace(start=lambda: None, join=lambda t=None: None, is_alive=lambda: True)))
    reviewer._INFLIGHT.clear()
    try:
        with kbc.connect_closing() as conn:
            for _ in range(reviewer.MAX_CONCURRENT + 1):
                _stopped(conn)
            assert len(reviewer.start_due(conn)) == reviewer.MAX_CONCURRENT
            assert reviewer.start_due(conn) == []  # every slot is still held
    finally:
        reviewer._INFLIGHT.clear()
        reviewer._THREADS.clear()


def test_a_decision_by_someone_else_resets_the_grant_cap(kanban_home, spend_reviewer):
    with kbc.connect_closing() as conn:
        tid, _ = _stopped(conn)
        with kb.write_txn(conn):
            for _ in range(reviewer.DEFAULT_MAX_GRANTS):
                kb._append_event(conn, tid, reviewer.GRANTED, {"episode": "old"})
        assert reviewer.consecutive_grants(conn, tid) == reviewer.DEFAULT_MAX_GRANTS
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "promoted_manual", {"actor": "david", "spend_budget": {"runs": 5}})
        assert reviewer.consecutive_grants(conn, tid) == 0


def test_the_backlog_ages_each_stop_by_its_own_notice(kanban_home, spend_reviewer, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, episode = _routed_with_reviewer_off(conn, monkeypatch)
        with kb.write_txn(conn):  # an older stop's notice must not make this one look stale
            kb._append_event(conn, tid, decision.NOTICE, {"episode": "older-stop", "producer": {"kind": "chats"}})
            conn.execute("UPDATE task_events SET created_at = created_at - 86400 WHERE task_id = ? AND "
                         "json_extract(payload, '$.episode') = 'older-stop'", (tid,))
        assert reviewer.start_due(conn, now=int(time.time()) + 60) == []
        assert reviewer.state(conn, tid, episode) is None


def test_a_grant_is_refused_once_the_reviewer_is_turned_off(kanban_home, spend_reviewer, inline, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)

        def judge_then_disable(ctx):
            monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
            return _grant()

        monkeypatch.setattr(reviewer, "judge", judge_then_disable)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert decision.NOTICE in _kinds(conn, tid)


def test_one_shot_dispatch_waits_for_its_reviews_and_finished_threads_are_dropped(monkeypatch):
    import threading
    done = threading.Event()
    thread = threading.Thread(target=lambda: done.wait(5) or done.set())
    thread.start()
    reviewer._THREADS[:] = [thread]
    done.set()
    reviewer.wait_for_reviews(timeout=5)
    assert reviewer._THREADS == [] and not thread.is_alive()


def test_a_dispatcher_tick_drops_finished_review_threads(kanban_home, spend_reviewer):
    finished = types.SimpleNamespace(is_alive=lambda: False)
    running = types.SimpleNamespace(is_alive=lambda: True)
    reviewer._THREADS[:] = [finished, running]
    try:
        with kbc.connect_closing() as conn:
            reviewer.start_due(conn)
        assert reviewer._THREADS == [running]
    finally:
        reviewer._THREADS.clear()


def test_the_one_shot_dispatch_command_waits_for_its_reviews(kanban_home, monkeypatch):
    from hermes_cli import kanban as kc
    waited = []
    monkeypatch.setattr(reviewer, "wait_for_reviews", lambda timeout=None: waited.append(1))
    kc.run_slash("dispatch --max 0")
    assert waited == [1]
