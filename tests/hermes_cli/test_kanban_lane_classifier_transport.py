"""Transport bounds and origin transaction integration for semantic lane placement."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lane_classifier as classifier
from hermes_cli import kanban_lanes as lanes
from tests.hermes_cli import test_kanban_lane_classifier as helpers
from tests.hermes_cli.test_kanban_origin_creation import intent

conn = helpers.conn
person = helpers.person
configured = helpers.configured
semantic = helpers.semantic


def test_helper_uses_stdin_timeout_and_subscription_fallback(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(classifier, "_subscription", lambda text: "research")
    pol = lanes.parse({"lanes": {"research": {"describe": "runs"}, "other": {}}})
    assert classifier.classify(pol, {"title": "H3", "body": "a" * 1500 + "SECRET_TAIL"}) == "research"
    argv, kw = calls[0]
    assert argv[-1] == "-" and argv[1] == "ask"
    assert kw["timeout"] == 5 and kw["encoding"] == "utf-8" and kw["check"] is True
    assert "SECRET_TAIL" not in kw["input"]
    assert json.loads(kw["input"].split("\n", 1)[1])["body"] == "a" * 1500


@pytest.mark.parametrize("answer,expected", [("hermes", "hermes"), ("not-a-lane", None)])
def test_helper_success_does_not_retry(monkeypatch, answer, expected):
    monkeypatch.setattr(classifier, "_pool_helper", lambda text: answer)
    monkeypatch.setattr(classifier, "_subscription", lambda text: pytest.fail("unnecessary retry"))
    pol = lanes.parse({"lanes": {"hermes": {}, "other": {}}})
    assert classifier.classify(pol, {}) == expected


def test_both_routes_fail_closed(monkeypatch):
    def fail(*args):
        raise OSError("offline")

    monkeypatch.setattr(classifier, "_pool_helper", fail)
    monkeypatch.setattr(classifier, "_subscription", fail)
    pol = lanes.parse({"lanes": {"other": {}}})
    assert classifier.classify(pol, {}) is None


def test_subscription_uses_managed_oauth_and_absolute_deadline(monkeypatch):
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as routing

    monkeypatch.setattr(routing, "managed_config", lambda: {"model_routing": {}})
    from agent import model_routing
    monkeypatch.setattr(model_routing, "eligible_routes", lambda *a: [
        {"provider": "openai-codex", "model": "fixture-model", "billing": "subscription"}])
    calls = []

    def call(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="research"))])

    monkeypatch.setattr(aux, "call_llm", call)
    assert classifier._subscription("classify") == "research"
    assert calls[0]["provider"] == "openai-codex"
    assert calls[0]["timeout"] == 5
    assert calls[0]["messages"] == [{"role": "user", "content": "classify"}]
    monkeypatch.setattr(routing, "managed_config", lambda: None)
    assert classifier._subscription("classify") is None
    assert len(calls) == 1


def test_origin_preflights_once_before_ledger_transaction(conn, semantic, monkeypatch):
    calls = []

    def classify(pol, opts):
        assert not conn.in_transaction
        calls.append(opts["title"])
        return "research"

    monkeypatch.setattr(lanes, "_fallback_lane", classify)
    from hermes_constants import get_hermes_home
    from tests.tools.test_kanban_create_overlap import register_chat
    chat = register_chat(get_hermes_home(), "20261123_120000_lane_owner")  # a real owning chat, not a bare ID
    tid = kb.create_task(conn, title="H3 recovery", assignee="default", origin_intent=intent(), session_id=chat)
    assert kb.get_task(conn, tid).tenant == "research"
    assert classifier.cached({}) is None
    assert calls == ["H3 recovery"]
    assert conn.execute("SELECT task_id FROM origin_creation_intents").fetchone()[0] == tid


def test_prepared_result_cannot_override_new_rule(conn, configured, monkeypatch):
    monkeypatch.setattr(lanes, "_fallback_lane", lambda *a: pytest.fail("rule called model"))
    with classifier.prepared("babybear", {"title": "work"}):
        tid = kb.create_task(conn, title="work", assignee="default", workspace_path="/src/hermes-agent")
    assert kb.get_task(conn, tid).tenant == "hermes"
