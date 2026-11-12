"""Hub pickup's judge answers live inside the gateway dispatcher (multiplexing, no turn scope). [daveey]

t_01a1cff283f89392: the dispatcher ticks in an empty Context, so on the multiplexing hub gateway the
judge's first credential read raised ``UnscopedSecretError`` and every cache-miss card was skipped as
"hub admission unavailable". Only a pre-warmed verdict cache (no credential read) got through.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import contextvars
import importlib
import json
import logging
from types import SimpleNamespace

import pytest

REPLY = {"admit": True, "need": "hub_chrome", "evidence": "signed-in Chrome", "why": "Needs the browser session."}


@pytest.fixture
def judge(tmp_path, monkeypatch):
    gate = importlib.import_module("hermes_cli.kanban_hub_admission")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_TOKEN=owner-token\n")
    skill = tmp_path / "skills/devops/hub-admission-test/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hub-admission-test\n---\nJudge the card.")
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    from agent import secret_scope as ss
    monkeypatch.setattr(managed, "managed_config", lambda: {"managed": True})
    seen = []

    def fake(**kwargs):  # like credential_pool._seed_anthropic_singletons: reads the token first
        seen.append(ss.get_secret("ANTHROPIC_TOKEN"))
        kwargs["route_info"].update(provider="anthropic", model="claude-sonnet-5-5", billing_group="subscription")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(REPLY)))])

    monkeypatch.setattr(aux, "call_llm", fake)
    ss.set_multiplex_active(True)
    try:
        yield gate, seen, ss
    finally:
        ss.set_multiplex_active(False)


def test_dispatcher_context_gets_a_live_verdict_under_the_owner_scope(judge):
    """The failing branch: an unscoped, empty-Context call on a multiplexing gateway (cache miss)."""
    gate, seen, ss = judge
    verdict = contextvars.Context().run(gate.admit, {"title": "Use signed-in Chrome", "body": "new card"})
    assert verdict.admit is True and verdict.cached is False
    assert seen == ["owner-token"]
    assert ss.current_secret_scope() is None  # the scope is the call's own, never left behind


def test_a_bound_turn_scope_is_kept(judge):
    gate, seen, ss = judge
    token = ss.set_secret_scope({"ANTHROPIC_TOKEN": "turn-token"})
    try:
        assert gate.admit({"title": "Use signed-in Chrome", "body": "turn card"}).admit is True
    finally:
        ss.reset_secret_scope(token)
    assert seen == ["turn-token"]


def test_unavailable_logs_the_branch(judge, monkeypatch, caplog):
    gate, _, _ = judge
    from agent import auxiliary_client as aux

    def fail(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(aux, "call_llm", fail)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_hub_admission"):
        verdict = gate.admit({"title": "Use signed-in Chrome", "body": "logged card"})
    assert verdict.why == "admission unavailable"
    assert "hub admission unavailable: RuntimeError: provider down" in caplog.text
