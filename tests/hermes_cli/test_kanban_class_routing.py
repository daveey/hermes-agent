"""Class-tier dispatch must retain deep review/retry and native worker credentials."""
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


def worker_args(task, monkeypatch):
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(dispatch, "_resolve_worker_cli_toolsets", lambda home: None)
    return dispatch._worker_argv(task, "default", None)


@pytest.mark.parametrize("case", ["review", "skill", "failure", "pattern", "routine"])
def test_class_tiers_keep_strong_work_deep(board, monkeypatch, case):
    cfg = {"default_model": "class:standard", "default_model_keep_patterns": ["security"]}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security" if case == "pattern" else "routine", assignee="default")
        if case == "failure":
            kb.claim_task(conn, tid)
            conn.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id=?", (tid,))
            conn.commit()
        task = kb.get_task(conn, tid)
        task.current_run_id = None
        if case == "skill":
            task.skills = ["sdlc-review"]
        tier.apply_model_tier(conn, task, lane="review" if case == "review" else "ready", cfg=cfg)
        assert kb.get_task(conn, tid).model_override is None
    argv = worker_args(task, monkeypatch)
    assert argv[argv.index("-m") + 1] == ("class:standard" if case == "routine" else "class:deep")
    assert argv[argv.index("--provider") + 1] == "auto"


def test_dispatch_tick_reads_class_default_and_keeps_review_deep(board, monkeypatch):
    from tests.hermes_cli.test_kanban_model_tier import _write_cfg
    _write_cfg(board, default_model="class:standard", default_provider="auto")
    seen = {}
    with kbc.connect() as conn:
        routine = kb.create_task(conn, title="routine", assignee="default")
        review = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, review)
        kb.request_review(conn, review, summary="ready")
        dispatch.dispatch_once(conn, dry_run=False,
                               spawn_fn=lambda task, workspace: seen.update({task.id: worker_args(task, monkeypatch)}))
        assert kb.get_task(conn, routine).model_override is None
        assert kb.get_task(conn, review).model_override is None
    for tid, model in ((routine, "class:standard"), (review, "class:deep")):
        argv = seen[tid]
        assert argv[argv.index("-m") + 1] == model
        assert argv[argv.index("--provider") + 1] == "auto"


def test_explicit_pin_survives_review_class_default(board, monkeypatch):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="review", assignee="default",
                            model_override="unknown-exact-model", provider_override="custom"))
        tier.apply_model_tier(conn, task, lane="review", cfg={"default_model": "class:standard"})
    argv = worker_args(task, monkeypatch)
    assert argv[argv.index("-m") + 1] == "unknown-exact-model"
    assert argv[argv.index("--provider") + 1] == "custom"


def test_invalid_class_provider_refused_before_launch(board, monkeypatch):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
    task.model_override, task.provider_override = "class:deep", "openrouter"
    with pytest.raises(ValueError, match="auto"):
        worker_args(task, monkeypatch)


@pytest.mark.parametrize("exhausted", [False, True])
def test_worker_generated_args_reach_native_wire(routing_home, streaming_route, native_transport,
                                                monkeypatch, tmp_path, exhausted):
    from types import SimpleNamespace
    from hermes_state import SessionDB
    task = SimpleNamespace(id="card", skills=[], reasoning_effort=None,
                           model_override="class:deep", provider_override=None)
    argv = worker_args(task, monkeypatch)
    model = argv[argv.index("-m") + 1]
    provider = argv[argv.index("--provider") + 1]
    if exhausted:
        monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    db = SessionDB(tmp_path / "worker.db")
    with patch("cli._prepare_deferred_agent_startup"), \
         patch("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build"), \
         patch("model_tools.get_tool_definitions", return_value=[]):
        shell = cli.HermesCLI(model=model, provider=provider, compact=True)
        shell.session_id = "kanban-worker-session"
        shell._session_db = db
        shell._install_tool_callbacks = lambda: None
        shell.streaming_enabled = False
        shell.ignore_rules = True
        if exhausted:
            assert not shell._init_agent()
            assert native_transport == []
        else:
            assert shell._init_agent()
            assert shell.agent.run_conversation("Say ok")["final_response"] == "ok"
            assert shell.agent.model_routing["requested_class"] == "deep"
            assert native_transport
            assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
                       and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
                       and "x-api-key" not in headers for url, headers, body in native_transport)
            shell.agent.release_clients()
    db.close()
