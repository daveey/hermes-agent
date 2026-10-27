"""Auxiliary class requirements: token units for context, best-effort JSON for titles."""
import pytest
from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


def _write(routing_home, tmp_path, **caps):
    from hermes_cli.config import atomic_config_write
    routing_home["model_routing"]["routes"]["deep"][0]["capabilities"].update(caps)
    atomic_config_write(tmp_path / "config.yaml", routing_home)


def test_context_bound_is_tokens_not_bytes(routing_home, streaming_route, native_transport, tmp_path):
    # ~300 KB of text is ~75k tokens: fits a 200k-token route, but its UTF-8 byte
    # count alone exceeds the cap, which refused every large aux job before.
    _write(routing_home, tmp_path, context_tokens=200_000)
    text = "word " * 60_000
    response = aux.call_llm("title", model="class:deep", max_tokens=32,
                            messages=[{"role": "user", "content": text}])
    assert response.choices[0].message.content == "ok"
    assert len(native_transport) == 1


def test_context_bound_still_refuses_oversized_requests(routing_home, streaming_route, native_transport,
                                                       tmp_path):
    _write(routing_home, tmp_path, context_tokens=20_000)
    with pytest.raises(ModelRoutingError):
        aux.call_llm("title", model="class:deep", max_tokens=32,
                     messages=[{"role": "user", "content": "word " * 60_000}])
    assert native_transport == []


def test_title_generation_json_is_best_effort(routing_home, streaming_route, native_transport, tmp_path):
    _write(routing_home, tmp_path, json=False)
    fmt = {"type": "json_schema", "json_schema": {"name": "t", "schema": {"type": "object"}}}
    response = aux.call_llm("title_generation", model="class:deep", max_tokens=32,
                            messages=[{"role": "user", "content": "hi"}], extra_body={"response_format": fmt})
    assert response.choices[0].message.content == "ok"
    assert len(native_transport) == 1
    _, _, body = native_transport[0]
    assert "response_format" not in body and "output_format" not in body


def test_other_tasks_still_require_attested_json(routing_home, streaming_route, native_transport, tmp_path):
    _write(routing_home, tmp_path, json=False)
    with pytest.raises(ModelRoutingError):
        aux.call_llm("title", model="class:deep", messages=[{"role": "user", "content": "hi"}],
                     extra_body={"response_format": {"type": "json_object"}})
    assert native_transport == []
