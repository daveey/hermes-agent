"""The hub judge accepts only an evidenced, allowlisted subscription verdict."""
import importlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def judge(tmp_path, monkeypatch):
    gate = importlib.import_module("hermes_cli.kanban_hub_admission")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill = tmp_path / "skills/devops/hub-admission-test/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hub-admission-test\n---\nJudge the card.")
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    monkeypatch.setattr(managed, "managed_config", lambda: {"managed": True})
    calls = []

    reply = {"admit": True, "need": "hub_chrome", "evidence": "signed-in Chrome",
             "why": "Needs the browser session."}

    def fake(**kwargs):
        calls.append(kwargs)
        kwargs["route_info"].update(provider="anthropic", model="claude-sonnet-5-5",
                                    billing_group="subscription")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])

    monkeypatch.setattr(aux, "call_llm", fake)
    return gate, calls, reply, skill


def test_valid_admission(judge):
    gate, calls, _, _ = judge
    verdict = gate.admit({"title": "Use signed-in Chrome", "body": "Read the page"})
    assert verdict.admit is True
    assert verdict.need == "hub_chrome"
    assert len(verdict.skill_sha) == 64
    assert calls[0]["provider"] == "anthropic"
    assert calls[0]["model"] == "claude-sonnet-5-5"
    assert calls[0]["timeout"] == 30
    assert "Judge the card." in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("change", [
    {"evidence": "invented quote"}, {"need": "hub_logs"}, {"evidence": ""},
    {"admit": "true"}, {"need": None}, {"why": []},
])
def test_invalid_verdict_denied(judge, change):
    gate, _, reply, _ = judge
    reply.update(change)
    result = gate.admit({"title": "Use signed-in Chrome"})
    assert result.admit is False
    assert result.why == "invalid admission verdict"


def test_cache_and_skill_revision(judge):
    gate, calls, _, skill = judge
    fields = {"title": "Use signed-in Chrome"}
    first = gate.admit(fields)
    second = gate.admit(fields)
    assert second.cached is True
    assert len(calls) == 1
    skill.write_text(skill.read_text() + "\nNew rubric rule.\n")
    third = gate.admit(fields)
    assert third.cached is False
    assert third.skill_sha != first.skill_sha
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("unavailable")])
def test_unavailable_denies_without_caching(judge, monkeypatch, failure):
    gate, calls, _, _ = judge
    from agent import auxiliary_client as aux
    fake = aux.call_llm

    def fail(**kwargs):
        raise failure

    monkeypatch.setattr(aux, "call_llm", fail)
    fields = {"title": "Use signed-in Chrome"}
    result = gate.admit(fields)
    assert result.admit is False
    assert result.why == "admission unavailable"
    monkeypatch.setattr(aux, "call_llm", fake)
    assert gate.admit(fields).admit is True
    assert len(calls) == 1


def test_malformed_json(judge, monkeypatch):
    gate, _, _, _ = judge
    from agent import auxiliary_client as aux
    fake = aux.call_llm

    def malformed(**kwargs):
        response = fake(**kwargs)
        response.choices[0].message.content = "```json not JSON```"
        return response

    monkeypatch.setattr(aux, "call_llm", malformed)
    result = gate.admit({"title": "Use signed-in Chrome"})
    assert result.admit is False
    assert result.why == "invalid admission verdict"
