"""The immediate spend-stop reviewer (``kanban_spend_reviewer``): a stopped card is judged at once instead of
waiting for an idle chat or a queued decision card, and only verifiable progress buys more runs."""
from __future__ import annotations

import json
import time
import types

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as ksb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli.kanban_release_comments import UnreadComments
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


def test_a_stop_is_queued_for_review_and_visible_without_a_decision_card(kanban_home, spend_reviewer):
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        assert reviewer.state(conn, tid, episode) == reviewer.QUEUED
        assert _kinds(conn, tid).count(decision.NOTICE) == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by = 'kanban-spend-budget'").fetchone()[0] == 0
        decision.safe_ensure_producer(conn, tid)  # queued review never suppresses/doubles owner visibility
        assert _kinds(conn, tid).count(decision.NOTICE) == 1


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
        comment = kb.list_comments(conn, tid)[-1]
        assert "Spend reviewer granted 5" in comment.body
        reference = granted['decision_comment']
        assert reference['comment_id'] == comment.id
        assert reference['author'] == reviewer.AUTHOR
        row = conn.execute('SELECT * FROM task_comments WHERE id=?', (comment.id,)).fetchone()
        if 'uid' in row.keys() and row['uid']:
            assert reference['comment_uid'] == row['uid']
        with pytest.raises(UnreadComments) as refused:
            ksb.set_override(conn, tid, runs=6, by='default', reason='reconcile')
        assert refused.value.comments[0]['id'] == comment.id
        ksb.set_override(conn, tid, runs=6, by='default', reason='reconcile', seen=comment.id)


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


def test_grants_are_capped_until_someone_else_decides(kanban_home, spend_reviewer, inline, monkeypatch):
    clock = int(time.time())
    monkeypatch.setattr(ksb.time, "time", lambda: clock)
    with kbc.connect_closing() as conn:
        tid, _ = _stopped(conn)
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
        from tests.hermes_cli.test_kanban_spend_route_repair import _historical_wrapper
        _historical_wrapper(conn, tid)  # compatibility with an already-created legacy decider
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
        from tests.hermes_cli.test_kanban_spend_route_repair import _historical_wrapper
        _historical_wrapper(conn, tid)  # replay history, never create a production guard
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


def _routing(*routes, allow_metered=False):
    """A managed subscription-only registry whose ``standard`` class lists ``routes``."""
    return {"model_routing": {
        "enabled": True, "policy_version": 1, "allow_metered": allow_metered, "enforce_subscription_only": True,
        "routes": {"deep": [], "fast": [], "standard": [
            {"provider": p, "model": m, "billing": b, "eligible": True, "funding_verified": True, "weight": 1,
             "capabilities": {"context_tokens": 200000, "tools": True, "streaming": True, "json": True}}
            for p, m, b in routes]}}}


_REGISTRY = (("anthropic", "claude-sonnet-5-5", "subscription"), ("openai-codex", "gpt-5.5", "subscription"),
             ("openrouter", "paid-model", "metered"))


def _meta(provider, model, billing="subscription", level="standard"):
    return {"provider": provider, "model": model, "requested_class": level, "selected_provider": provider,
            "selected_model": model, "billing_group": billing, "policy_version": 1}


def _judge_with(monkeypatch, route, config):
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    calls = []

    def call_llm(**kw):
        assert kw["model"] == "class:standard" and "provider" not in kw
        assert kw["task"] == "kanban_spend_review"
        calls.append(kw)
        kw["route_info"].update(route)
        message = types.SimpleNamespace(content='{"decision": "escalate", "reason": "same wait"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(aux, "call_llm", call_llm)
    monkeypatch.setattr(managed, "managed_config", lambda config_=None: config)
    return reviewer.judge({"_cap": 3, "_corpus": "", "title": "t"}), calls


@pytest.mark.parametrize("route", [
    _meta("anthropic", "claude-sonnet-5-5"),
    _meta("openai-codex", "gpt-5.5"),  # any eligible subscription route of the class, not one concrete model
])
def test_the_judge_accepts_any_eligible_subscription_route_of_its_class(monkeypatch, route):
    verdict, calls = _judge_with(monkeypatch, route, _routing(*_REGISTRY))
    assert verdict == {"decision": "escalate", "reason": "same wait"}
    assert [(c["model"], c.get("provider")) for c in calls] == [(reviewer.ROUTE_CLASS, None)]


@pytest.mark.parametrize("route", [
    {"provider": "anthropic", "model": "claude-sonnet-5-5"},  # no billing evidence
    _meta("openrouter", "paid-model", "metered"),  # a paid route never answers
    _meta("anthropic", "claude-sonnet-5-5", level="deep"),  # another class
    _meta("anthropic", "unlisted-model"),  # claims subscription but is not in the registry
    {**_meta("openai-codex", "gpt-5.5"), "selected_model": "claude-sonnet-5-5"},  # identity mismatch
])
def test_the_judge_refuses_a_route_that_is_not_an_eligible_subscription_member(monkeypatch, route):
    assert _judge_with(monkeypatch, route, _routing(*_REGISTRY))[0] is None


def test_the_judge_refuses_a_route_withdrawn_from_the_registry(monkeypatch):
    route = _meta("openai-codex", "gpt-5.5")
    assert _judge_with(monkeypatch, route, _routing(("anthropic", "claude-sonnet-5-5", "subscription")))[0] is None


def test_without_subscription_only_routing_the_judge_never_invokes_a_model(monkeypatch):
    verdict, calls = _judge_with(monkeypatch, _meta("anthropic", "claude-sonnet-5-5"), None)
    assert verdict is None and calls == []


def test_an_unavailable_subscription_class_escalates(monkeypatch):
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    from agent.model_routing import ModelRoutingError

    def call_llm(**kw):
        raise ModelRoutingError("No eligible standard routes under billing/capability policy")

    monkeypatch.setattr(aux, "call_llm", call_llm)
    monkeypatch.setattr(managed, "managed_config", lambda config=None: _routing())
    with pytest.raises(ModelRoutingError):  # review() turns any judge exception into an escalation
        reviewer.judge({"_cap": 3, "_corpus": "", "title": "t"})


def test_the_class_allocator_offers_only_subscription_routes_before_any_call():
    from agent.model_routing import eligible_routes
    policy = _routing(*_REGISTRY, allow_metered=True)["model_routing"]  # enforce_subscription_only wins
    assert {r["billing"] for r in eligible_routes(policy, "standard")} == {"subscription"}


@pytest.mark.parametrize("failure", ["paid", "missing-billing", "unavailable", "timeout", "non-json"])
def test_real_judge_failure_escalates_without_a_grant(kanban_home, monkeypatch, failure):
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 2))
    monkeypatch.setattr(managed, "managed_config", lambda config=None: _routing(*_REGISTRY))
    calls, parsed = [], []
    real_parse = reviewer.parse

    def parse(content):
        parsed.append(content)
        return real_parse(content)

    monkeypatch.setattr(reviewer, "parse", parse)

    def call_llm(**kwargs):
        calls.append((kwargs["task"], kwargs["model"], kwargs.get("provider")))
        if failure == "unavailable":
            raise RuntimeError("no eligible route")
        if failure == "timeout":
            raise TimeoutError("deadline")
        route = _meta("openai-codex", "gpt-5.5")
        if failure == "missing-billing":
            route.pop("billing_group")
        elif failure == "paid":
            route["billing_group"] = "paid"
        kwargs["route_info"].update(route)
        content = "not json" if failure == "non-json" else json.dumps(_grant())
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])

    monkeypatch.setattr(aux, "call_llm", call_llm)
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": episode})
        assert reviewer.review(conn, tid, episode) == "escalated"
        assert calls == [("kanban_spend_review", reviewer.ROUTE_CLASS, None)]
        assert parsed == (["not json"] if failure == "non-json" else [])
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.GRANTED not in _kinds(conn, tid)
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED


def test_a_hung_judge_is_abandoned_at_the_deadline(monkeypatch):
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    import threading
    release = threading.Event()
    monkeypatch.setattr(reviewer, "TIMEOUT", 1)
    monkeypatch.setattr(managed, "managed_config", lambda config=None: _routing(*_REGISTRY))
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


@pytest.mark.parametrize("cause", [1, True, ["scope_stopped"], {"safety_refusal": True}],
                         ids=["int", "bool", "list", "dict"])
def test_malformed_cause_does_not_block_healthy_reviews(kanban_home, spend_reviewer, inline, monkeypatch, caplog,
                                                       cause):
    # Three stopped cards per case: borrow the shared run clock so later tests keep their past-run budget.
    monkeypatch.setitem(helpers._CLOCK, "t", 0)
    with kbc.connect_closing() as conn:
        bad, bad_episode = _stopped(conn)  # queued and noticed, then its stop cause is corrupted
        with kb.write_txn(conn):
            kb._append_event(conn, bad, decision.EVENT, {"cause": cause})
        with pytest.raises(ValueError, match="cause"):
            decision.pending_episode(conn, bad)
        good, good_episode = _stopped(conn)
        routed, routed_episode = _routed_with_reviewer_off(conn, monkeypatch)
        now = int(time.time()) + reviewer.review_after_seconds() + 5
        assert reviewer.queue_backlog(conn, now=now, cfg=(True, 2)) == [routed]
        assert sorted(reviewer.start_due(conn, now=now)) == sorted([good, routed])
        for tid, episode in ((good, good_episode), (routed, routed_episode)):
            assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED  # judge unavailable: owner route
        assert kb.get_task(conn, bad).status == "triage"
        assert reviewer.state(conn, bad, bad_episode) == reviewer.QUEUED  # no lease, grant or release
        assert bad in caplog.text
        assert not conn.in_transaction


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


def test_three_spent_runs_buy_exactly_two_more(kanban_home, spend_reviewer, inline, monkeypatch):
    clock = int(time.time())
    monkeypatch.setattr(ksb.time, "time", lambda: clock)
    spend_reviewer.append(_grant(runs=2))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Bounded progress", assignee="default")
        ksb.set_override(conn, tid, runs=3, by="owner", reason="initial allowance")
        helpers._runs(conn, tid, 3)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET summary=? WHERE task_id=?", (PROGRESS, tid))
        assert ksb.spend(conn, tid).runs == 3
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "ready"
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) is None
        for count in (1, 2):
            clock += 100
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
                             "VALUES (?, 'default', 'done', ?, ?, 'continued', ?)",
                             (tid, clock - 90, clock, PROGRESS))
            assert ksb.spend(conn, tid).runs == count
            assert ksb.hold(conn, tid, lim=helpers.LIMITS) == (None if count == 1 else "triage")


@pytest.mark.parametrize("during_judge", [False, True])
@pytest.mark.parametrize("writer_kind", ["human", "agent"])
def test_a_post_stop_override_is_never_overwritten(kanban_home, spend_reviewer, inline, monkeypatch,
                                                  during_judge, writer_kind):
    with kbc.connect_closing() as conn:
        tid, episode = _stopped(conn)

        def decide():
            ksb.set_override(conn, tid, runs=15, by="owner", reason="owner decision", writer_kind=writer_kind)

        def judge(ctx):
            if during_judge:
                decide()
            return _grant(runs=2)

        if not during_judge:
            decide()
        monkeypatch.setattr(reviewer, "judge", judge)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert ksb.card_override(conn, tid)["by"] == "owner"
        assert ksb.card_override(conn, tid)["runs"] == 15
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert _kinds(conn, tid).count(ksb.OVERRIDE_EVENT) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_zero_run_retriage_never_grants_again(kanban_home, spend_reviewer, inline, legacy):
    spend_reviewer.append(_grant(runs=2))
    with kbc.connect_closing() as conn:
        tid, _ = _stopped(conn)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "ready"
        with kb.write_txn(conn):
            if legacy:  # historical grants lacked a fresh-window marker
                conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='promoted_manual'",
                             ('{"actor":"kanban-spend-reviewer"}', tid))
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, ksb.EVENT, {"limit": "runs", "runs": 3, "since": 0,
                                                  "limits": {"runs": 2}})
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        spend_reviewer.append(_grant(runs=2))
        reviewer.queue(conn, tid, episode)
        for _ in range(3):
            reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert _kinds(conn, tid).count(reviewer.GRANTED) == 1
        assert _kinds(conn, tid).count("promoted_manual") == 1


def test_zero_run_fence_uses_the_latest_of_two_promotions(kanban_home, spend_reviewer, inline, monkeypatch):
    clock = int(time.time())
    monkeypatch.setattr(ksb.time, "time", lambda: clock)
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 3))  # cap must not mask the fence
    with kbc.connect_closing() as conn:
        tid, _ = _stopped(conn)
        for grant in range(2):
            spend_reviewer.append(_grant(runs=2))
            reviewer.start_due(conn)
            assert kb.get_task(conn, tid).status == "ready"
            if grant == 0:
                for _ in range(2):
                    clock += 100
                    with kb.write_txn(conn):
                        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                                     "summary) VALUES (?, 'default', 'done', ?, ?, 'continued', ?)",
                                     (tid, clock - 90, clock, PROGRESS))
                clock += 10
                assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        clock += 10
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, ksb.EVENT, {"limit": "runs", "runs": 2, "since": 0,
                                                  "limits": {"runs": 2}})
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        spend_reviewer.append(_grant(runs=2))
        reviewer.queue(conn, tid, episode)
        for _ in range(3):
            reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert _kinds(conn, tid).count(reviewer.GRANTED) == 2
        assert _kinds(conn, tid).count("promoted_manual") == 2


def test_plain_manual_promotion_with_zero_runs_also_requires_the_owner(
        kanban_home, spend_reviewer, inline):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Budget spent while blocked", assignee="default")
        helpers._runs(conn, tid, ksb.DEFAULT_RUNS)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET summary=? WHERE task_id=?", (PROGRESS, tid))
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        kb.promote_task(conn, tid, actor="owner")  # no spend-budget fresh window
        assert kb.get_task(conn, tid).status == "ready"
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        spend_reviewer.append(_grant(runs=2))
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
        assert _kinds(conn, tid).count(reviewer.GRANTED) == 0
        assert _kinds(conn, tid).count("promoted_manual") == 1


@pytest.mark.parametrize("offset, inserted_after, grants", [
    (-1, False, False), (0, False, False), (0, True, False), (1, True, True),
])
def test_promotion_run_boundary_fails_closed_on_same_second(
        kanban_home, spend_reviewer, inline, monkeypatch, offset, inserted_after, grants):
    clock = int(time.time())
    monkeypatch.setattr(ksb.time, "time", lambda: clock)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Promotion boundary", assignee="default")
        helpers._runs(conn, tid, 1)  # real spend; the boundary row may be a short, already-ended attempt
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))

        def spent_run():
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                             "summary) VALUES (?, 'default', 'done', ?, ?, 'continued', ?)",
                             (tid, clock + offset, clock + offset, PROGRESS))

        if not inserted_after:
            spent_run()
        kb.promote_task(conn, tid, actor="owner")
        if inserted_after:
            spent_run()
        clock += 10
        limits = ksb.Limits(runs=1, cache_read_tokens=0)
        assert ksb.hold(conn, tid, lim=limits) == "triage"
        episode = reviewer.episode_of(decision.pending_episode(conn, tid))
        spend_reviewer.append(_grant(runs=1))
        for _ in range(3):
            reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == ("ready" if grants else "triage")
        assert reviewer.state(conn, tid, episode) == (reviewer.GRANTED if grants else reviewer.ESCALATED)
        assert _kinds(conn, tid).count(reviewer.GRANTED) == int(grants)


def test_the_one_shot_dispatch_command_waits_for_its_reviews(kanban_home, monkeypatch):
    from hermes_cli import kanban as kc
    waited = []
    monkeypatch.setattr(reviewer, "wait_for_reviews", lambda timeout=None: waited.append(1))
    kc.run_slash("dispatch --max 0")
    assert waited == [1]
