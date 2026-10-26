"""Resuming an existing row must not adopt the current transport's login."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


@pytest.mark.parametrize("owner", [None, "basic:original-owner"])
@pytest.mark.parametrize("mode", ["cold", "deferred", "eager", "lazy"])
def test_resume_preserves_persisted_owner(monkeypatch, tmp_path, owner, mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("stored", source="desktop", user_id=owner)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    # No workers, agents, live dispatch, or external services in this regression.
    for name in ("_enable_gateway_prompts", "_schedule_agent_build",
                 "_schedule_session_cap_enforcement", "_maybe_schedule_auto_continue",
                 "_schedule_resume_hydration", "_wire_session_agent", "_start_session_services",
                 "_schedule_mcp_late_refresh", "_emit", "_register_session_cwd",
                 "_hydrate_session_cwd"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda row: {})
    monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
    monkeypatch.setattr(server, "_child_run_active", lambda *a: False)
    agent_factory = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(server, "_make_agent_in_context", agent_factory)
    ctx = server._Resume(1, {"source": "desktop"}, "stored")
    ctx.db, ctx.found = db, db.get_session("stored")
    ctx.profile_resume_cwd = str(tmp_path)
    ctx.lazy = mode == "lazy"
    monkeypatch.setattr(ctx, "info", lambda *a: {})
    monkeypatch.setattr(server, "_lazy_resume_info", lambda *a, **k: {})
    transport = SimpleNamespace(auth_identity={"provider": "basic", "user_id": "resumer"})
    token = bind_transport(transport)
    try:
        response = getattr(server, "_resume_" + mode)(ctx)
        assert "error" not in response, response
        record = server._sessions[response["result"]["session_id"]]
        assert record["auth_user_id"] == owner
        assert server._session_auth_user_id(record) == owner
        assert db.get_session("stored")["user_id"] == owner
        if mode == "eager":
            assert agent_factory.call_args.kwargs["auth_user_id"] == owner
        else:
            agent_factory.assert_not_called()
    finally:
        reset_transport(token)
        db.close()


def test_new_eager_session_keeps_transport_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_sessions", {})
    for name in ("_session_todo_state", "_hydrate_session_cwd", "_register_session_cwd",
                 "_wire_session_agent", "_start_session_services", "_emit", "_schedule_mcp_late_refresh"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    token = bind_transport(SimpleNamespace(auth_identity={"provider": "basic", "user_id": "creator"}))
    try:
        server._init_session("new", "new-key", SimpleNamespace(), [], cwd=str(tmp_path))
        assert server._sessions["new"]["auth_user_id"] == "basic:creator"
    finally:
        reset_transport(token)
