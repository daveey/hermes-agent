"""Class-tier dispatch must retain deep review/retry and native worker credentials.

Fork: upstream moved card execution to the profile owner. The dispatcher's worker is now
``-m hermes_cli.kanban_worker_client`` (no ``-m <model> --provider <p>`` argv); the owner re-reads the
card and builds the turn's policy from ``kanban_model_tier.owner_model_pin`` (gateway.session_kanban),
then the gateway builds the agent (``tui_gateway`` ``_make_agent``). These tests assert the same intent
(class tiers, deep review/retry, provider ``auto``, native subscription wire) on that path.
"""
from unittest.mock import patch

import pytest
import cli  # bootstrap before the real-home guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli import test_kanban_model_tier
from tests.tui_gateway import test_class_routing
from tests.agent import test_model_routing_agent

board = test_kanban_model_tier.board
native_transport = test_class_routing.native_transport
routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


@pytest.fixture
def routable(monkeypatch):
    """These tests are about which class a card gets on a host that can route every class.
    The unroutable-host fallback is covered in test_kanban_class_unroutable.py."""
    monkeypatch.setattr(tier, "class_unroutable_reason", lambda model, config=None: None)


def owner_pin(task, conn=None):
    """``(model, provider)`` the profile owner runs *task* with (formerly the worker argv's -m/--provider)."""
    if conn is not None:
        return tier.owner_model_pin(conn, task)
    with kbc.connect_closing() as own:
        return tier.owner_model_pin(own, task)


def gateway_agent(monkeypatch, model, provider):
    """Build the agent the gateway runs the owner's turn on, from the pinned ``(model, provider)``."""
    from hermes_cli.config import load_config
    from tui_gateway import server
    monkeypatch.setattr(server, "_load_cfg", load_config)
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform: [])
    monkeypatch.setattr(server, "_startup_system_prompt", lambda *a: "")
    monkeypatch.setattr(server, "_get_db", lambda: None)
    return server._make_agent("kanban-ui", "kanban-worker-session", model_override=model,
                              provider_override=provider)


@pytest.mark.parametrize("case", ["review", "skill", "failure", "pattern", "routine"])
def test_class_tiers_keep_strong_work_deep(board, routable, monkeypatch, case):
    cfg = {"default_model": "class:standard", "default_model_keep_patterns": ["security"]}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security" if case == "pattern" else "routine", assignee="default")
        if case in ("review", "skill"):
            test_kanban_model_tier._review_source(conn, tid)
            if case == "skill":
                kb.complete_task(conn, tid, summary="Source implementation complete")
                tid = kb.create_task(conn, title="Independent review", assignee="default",
                                     creator_task_id=tid, skills=["sdlc-review"])
        if case == "failure":
            kb.claim_task(conn, tid)
            conn.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id=?", (tid,))
            conn.commit()
        task = kb.get_task(conn, tid)
        if case == "review":
            task = kb.claim_review_task(conn, tid)
        elif case == "skill":
            task = kb.claim_task(conn, tid)
        else:
            task.current_run_id = None
        if case == "skill":
            task.skills = ["sdlc-review"]
        tier.apply_model_tier(conn, task, lane="review" if case == "review" else "ready", cfg=cfg)
        assert kb.get_task(conn, tid).model_override is None
        model, provider = owner_pin(task, conn)
    if case in ("review", "skill"):
        assert (model, provider) == (test_kanban_model_tier.OPUS, "anthropic")
    else:
        assert model == ("class:standard" if case == "routine" else "class:deep")
        assert provider == "auto"


def test_dispatch_tick_reads_class_default_and_keeps_review_deep(board, routable, monkeypatch):
    from tests.hermes_cli.test_kanban_model_tier import _write_cfg
    _write_cfg(board, default_model="class:standard", default_provider="auto")
    seen = {}
    with kbc.connect() as conn:
        review = kb.create_task(conn, title="review", assignee="default")
        test_kanban_model_tier._review_source(conn, review)
        routine = kb.create_task(conn, title="routine", assignee="default")
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, workspace: seen.update({task.id: task}))
        assert kb.get_task(conn, routine).model_override is None
        assert kb.get_task(conn, review).model_override is None
        # The owner re-reads the card from the board (not the dispatcher's in-memory task).
        pins = {tid: owner_pin(kb.get_task(conn, tid), conn) for tid in seen}
    assert pins[routine] == ("class:standard", "auto")
    assert pins[review] == (test_kanban_model_tier.OPUS, "anthropic")


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_legacy_concrete_lane_pin_is_ignored_for_class_default(board, monkeypatch, lane):
    """Concrete pins/handoff models are retired: refused at ingress, ignored if found on an old row."""
    with kbc.connect() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="review", assignee="default", model_override="unknown-exact-model",
                           provider_override="custom")
        tid = kb.create_task(conn, title="review", assignee="default")
        # Historical author row, seeded raw in this isolated DB; its provenance must survive dispatch.
        conn.execute("UPDATE tasks SET model_override=?, provider_override='custom' WHERE id=?",
                     ("author-model" if lane == "review" else "unknown-exact-model", tid))
        conn.commit()
        task = kb.get_task(conn, tid)
        if lane == "review":
            kb.claim_task(conn, task.id)
            ok, reason = kb.request_review(conn, task.id, metadata={
                "review_model": "unknown-exact-model", "review_provider": "custom"}, with_reason=True)
            assert not ok and 'model-neutral' in reason
            kb.request_review(conn, task.id, metadata={"review_model": "class:deep"})
            task = kb.claim_review_task(conn, task.id)
        tier.apply_model_tier(conn, task, lane=lane, cfg={"default_model": "class:standard"})
        # The review class resolves to a concrete execution receipt (never the legacy author-model pin).
        assert owner_pin(task, conn) == (("claude-opus-5-5", "anthropic") if lane == "review"
                                         else ("class:standard", "auto"))
        assert kb.get_task(conn, tid).provider_override == "custom"


def test_class_with_concrete_provider_is_refused_at_ingress_and_ignored_at_launch(board, monkeypatch):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        with pytest.raises(ValueError):
            kb.create_task(conn, title="bad", assignee="default", model_override="class:deep",
                           provider_override="openrouter")
        task.model_override, task.provider_override = "class:deep", "openrouter"
        # A stale concrete provider can never steer a class: the launch is always ``auto``.
        assert owner_pin(task, conn) == ("class:deep", "auto")


@pytest.mark.parametrize("exhausted", [False, True])
def test_worker_generated_args_reach_native_wire(routing_home, streaming_route, native_transport,
                                                monkeypatch, tmp_path, exhausted):
    from types import SimpleNamespace
    from agent.model_routing import ModelRoutingError
    monkeypatch.chdir(tmp_path)
    task = SimpleNamespace(id="card", skills=[], reasoning_effort=None, current_run_id=None,
                           model_override="class:deep", provider_override=None)
    model, provider = owner_pin(task)
    assert (model, provider) == ("class:deep", "auto")
    if exhausted:
        monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    with patch("model_tools.get_tool_definitions", return_value=[]):
        if exhausted:
            with pytest.raises(ModelRoutingError):
                gateway_agent(monkeypatch, model, provider)
            assert native_transport == []
        else:
            agent = gateway_agent(monkeypatch, model, provider)
            assert agent.run_conversation("Say ok")["final_response"] == "ok"
            assert agent.model_routing["requested_class"] == "deep"
            assert native_transport
            assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
                       and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
                       and "x-api-key" not in headers for url, headers, body in native_transport)
            agent.release_clients()
