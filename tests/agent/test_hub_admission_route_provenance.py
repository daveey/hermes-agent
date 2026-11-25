"""Hub admission provenance through the real auxiliary router (no fake route_info). [daveey]

Review t_01a1ceabf1ca19c8 (blocker 1): the judge fixture filled ``route_info['billing_group']``
itself, hiding whether production ``call_llm`` reports it. These tests drive the real
``agent.auxiliary_client.call_llm`` (class routing, managed subscription fence, native Anthropic
SDK) and fake only the HTTP transport, so a verdict is honoured only when the router itself proves
a configured subscription route — and an unmanaged host never sends at all.
"""
import json

import pytest

from hermes_cli import kanban_hub_admission as judge
from hermes_cli.config import atomic_config_write
from tests.agent import test_model_routing_agent as agent_fixtures

routing_home = agent_fixtures.routing_home
streaming_route = agent_fixtures.streaming_route
QUOTE = "Read the page in the hub's signed-in Chrome"
RUBRIC = "---\nname: hub-admission-test\ndescription: test rubric\n---\nAnswer with strict JSON.\n"


@pytest.fixture
def wire(monkeypatch):
    anthropic = pytest.importorskip("anthropic", reason="native Anthropic wire test needs the SDK")
    import httpx
    sent, reply = [], {"text": json.dumps({"admit": True, "need": "hub_chrome", "evidence": QUOTE,
                                           "why": "needs the hub's signed-in Chrome"})}

    def respond(request):
        sent.append((dict(request.headers), json.loads(request.content)))
        events = [
            {"type": "message_start", "message": {
                "id": "t", "type": "message", "role": "assistant", "model": "anthropic-deep",
                "content": [], "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply["text"]}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"}]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    original = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: original(
        **kw, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    monkeypatch.setattr("agent.title_generator._auto_title_enabled", lambda: False)
    return sent, reply


@pytest.fixture
def managed(routing_home, streaming_route, tmp_path, monkeypatch):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    routing_home["auxiliary"] = {"hub_admission": {"model": "class:deep"}}
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    skill = tmp_path / "skills" / "devops" / "hub-admission-test" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(RUBRIC, encoding="utf-8")
    return routing_home


def test_real_router_reports_subscription_provenance(managed, wire):
    sent, _ = wire
    route = {}
    from agent import auxiliary_client as aux
    aux.call_llm(task="hub_admission", max_tokens=64,
                 temperature=0, route_info=route, messages=[{"role": "user", "content": "x"}])
    assert (route.get("provider"), route.get("model"), route.get("billing_group")) == (
        "anthropic", "anthropic-deep", "subscription")
    assert sent[0][0]["authorization"].startswith("Bearer sk-ant-oat")


def test_real_router_admit_is_honoured(managed, wire):
    sent, _ = wire
    verdict = judge.admit({"title": QUOTE, "body": "Signed-in browser work"})
    assert (verdict.admit, verdict.need, verdict.evidence) == (True, "hub_chrome", QUOTE)
    assert verdict.why != "admission unavailable"
    assert len(sent) == 1 and sent[0][1]["model"] == verdict.model == "anthropic-deep"


def test_real_router_without_subscription_fence_never_sends(routing_home, streaming_route, tmp_path,
                                                            monkeypatch, wire):
    """An unmanaged host (no enforce_subscription_only) would route through the legacy resolver,
    which reports no billing group and may fall back to another model: deny without sending."""
    sent, _ = wire

    verdict = judge.admit({"title": QUOTE, "body": ""})
    assert verdict.admit is False and verdict.why == "admission unavailable"
    assert sent == []


def test_real_router_paid_only_route_is_denied(managed, wire, tmp_path):
    """A configured class the subscription fence cannot serve is refused before any send."""
    sent, _ = wire
    managed["model_routing"]["routes"]["deep"][0]["billing"] = "paid"
    atomic_config_write(tmp_path / "config.yaml", managed)
    verdict = judge.admit({"title": QUOTE, "body": ""})
    assert verdict.admit is False and verdict.why == "admission unavailable"
    assert sent == []
