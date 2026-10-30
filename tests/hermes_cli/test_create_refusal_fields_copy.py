"""A refused ``session.create`` names the field, and the CLI's own labels pass the create contract.

The canonical contract check refuses with ``data.fields`` (a list); the CLI copy read only
``data.field``/``data.detail``, so ``--source peer-mail`` printed a bare ``invalid_params`` under
"couldn't start the model connection" and sent the investigation to model routing.
"""

from types import SimpleNamespace

import pytest

from hermes_cli._parser import build_top_level_parser


def test_refusal_text_renders_a_fields_list():
    from hermes_cli.cli_chat_error_copy import agent_init_failure_message, refusal_text
    from hermes_cli.gateway_client import GatewayRPCError, rpc_error

    # The exact frame gateway/session_controls.dispatch sends for a contract refusal.
    exc = rpc_error({"code": 4001, "message": "invalid_params",
                     "data": {"reason": "invalid_params", "fields": ["source"]}})
    assert refusal_text(exc) == "invalid_params (source)"
    assert "invalid_params (source)" in agent_init_failure_message(exc)
    two = GatewayRPCError("invalid_params", 4001, {"reason": "invalid_params", "fields": ["source", "params.bogus"]})
    assert refusal_text(two) == "invalid_params (source, params.bogus)"
    # A field/detail refusal still reads as before, and an empty list adds no parentheses.
    single = GatewayRPCError("invalid_params", 4001, {"field": "toolsets", "detail": "unknown"})
    assert refusal_text(single) == "invalid_params (toolsets: unknown)"
    assert refusal_text(GatewayRPCError("invalid_params", 4001, {"fields": []})) == "invalid_params"


class _FakeConnection:
    def __init__(self):
        self.created = []

    def call(self, method, **params):
        if method == "runtime.describe":
            return {"session_create": {"sources": ["cli"], "parameters": []}}
        assert method == "session.create"
        self.created.append(params)
        return {"stored_session_id": "s1", "info": {}}

    def state(self, sid):
        return SimpleNamespace(adopt_snapshot=lambda snapshot: None)


@pytest.mark.parametrize("given, stored", [("tool", "tool"), ("desktop", "gui"), ("oneshot", "oneshot")])
def test_cli_sources_create_a_session_the_contract_accepts(monkeypatch, given, stored):
    """`hermes chat -Q --oneshot --source tool|desktop` parses, and the create it drives passes the
    canonical ``session.create`` check the gateway dispatcher runs."""
    from hermes_cli.gateway_session_store import GatewaySessionStore
    from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems

    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["chat", "-Q", "--oneshot", "-q", "hi", "--source", given])
    # What cmd_chat does with --source before the store creates the row.
    monkeypatch.setenv("HERMES_SESSION_SOURCE", args.source)
    monkeypatch.setenv("HERMES_SESSION_SOURCE_EXPLICIT", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    connection = _FakeConnection()
    assert GatewaySessionStore(connection).create({}) == "s1"
    (params,) = connection.created
    assert params["source"] == stored
    assert canonical_param_problems(CANONICAL_METHODS["session.create"], params) == []
