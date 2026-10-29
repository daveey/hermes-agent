"""A class request on a reused API session must serve its own class, never the
class the session was first allocated for (implicit fingerprint ids and explicit
X-Hermes-Session-Id continuation alike)."""
from unittest.mock import patch

import pytest

from tests.gateway.test_api_class_routing import adapter, native_transport, routing_home


@pytest.fixture
def all_classes_stream(routing_home, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home["model"] = {"context_length": 100000}
    for routes in routing_home["model_routing"]["routes"].values():
        for route in routes:
            route["capabilities"]["streaming"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)


async def _post_classes(adapter, headers):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    session_ids = []
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        async with TestClient(TestServer(app)) as client:
            for level in ("fast", "standard", "deep"):
                response = await client.post("/v1/chat/completions", headers=headers, json={
                    "model": "class:" + level, "provider": "auto", "stream": False,
                    "messages": [{"role": "user", "content": "Reply with exactly: ROUTE_OK"}]})
                text = await response.text()
                assert response.status == 200, text
                session_ids.append(response.headers.get("X-Hermes-Session-Id"))
    return session_ids


def _served_models(native_transport):
    return [body["model"] for _, _, body in native_transport]


@pytest.mark.asyncio
async def test_implicit_session_serves_each_requested_class(
        adapter, routing_home, all_classes_stream, native_transport):
    session_ids = await _post_classes(adapter, {})
    assert len(set(session_ids)) == 1 and session_ids[0].startswith("api-")  # same derived session
    assert _served_models(native_transport) == ["anthropic-fast", "anthropic-standard", "anthropic-deep"]


@pytest.mark.asyncio
async def test_explicit_session_header_serves_each_requested_class(
        adapter, routing_home, all_classes_stream, native_transport):
    adapter._api_key = "test-key"
    session_ids = await _post_classes(adapter, {
        "Authorization": "Bearer test-key", "X-Hermes-Session-Id": "explicit-class-session"})
    assert session_ids == ["explicit-class-session"] * 3
    assert _served_models(native_transport) == ["anthropic-fast", "anthropic-standard", "anthropic-deep"]
