"""execute_code child env carries the turn's session id/key. [daveey]

A sandbox script that shells out to ``hermes kanban create`` must stamp the owning
chat (the CLI reads HERMES_SESSION_KEY / HERMES_SESSION_ID), exactly like the
terminal tool; otherwise the cards never appear in that chat's task strip or
sidebar badge. Only these two non-secret ids are bridged, and in a multiplexed
process the ContextVar wins over a possibly-foreign ``os.environ`` mirror.
"""

import sys

import pytest

import gateway.session_context as sc
from tools.code_execution_env import _build_child_env


def _child_env():
    return _build_child_env(rpc_endpoint="sock", rpc_token="tok",
                            tmpdir="/tmp/hermes-test", child_python=sys.executable)


@pytest.fixture
def bound_session():
    tokens = []

    def _bind(**kw):
        tokens.append(sc.set_session_vars(**kw))

    yield _bind
    for t in reversed(tokens):
        sc.clear_session_vars(t)


def test_bound_session_ids_reach_child(bound_session, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign_session")
    monkeypatch.setenv("HERMES_SESSION_KEY", "foreign_session")
    bound_session(session_id="20261016_185414_c283c2", session_key="20261016_185414_c283c2",
                  chat_name="evil; rm -rf /", source="desktop")
    env = _child_env()
    assert env["HERMES_SESSION_ID"] == "20261016_185414_c283c2"
    assert env["HERMES_SESSION_KEY"] == "20261016_185414_c283c2"
    # Only the identity ids are bridged; other session vars stay scrubbed.
    assert "HERMES_SESSION_CHAT_NAME" not in env
    assert "HERMES_SESSION_SOURCE" not in env


def test_engaged_but_cleared_context_does_not_leak_mirror(bound_session, monkeypatch):
    bound_session(session_id="", session_key="")
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign_session")
    monkeypatch.setenv("HERMES_SESSION_KEY", "foreign_session")
    env = _child_env()
    assert "HERMES_SESSION_ID" not in env
    assert "HERMES_SESSION_KEY" not in env


def test_unengaged_cli_falls_back_to_environ(monkeypatch):
    monkeypatch.setattr(sc, "_session_context_engaged", False)
    tok = [v.set(sc._UNSET) for v in (sc._VAR_MAP["HERMES_SESSION_ID"], sc._VAR_MAP["HERMES_SESSION_KEY"])]
    try:
        monkeypatch.setenv("HERMES_SESSION_ID", "cli_session")
        monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
        env = _child_env()
        assert env["HERMES_SESSION_ID"] == "cli_session"
        assert "HERMES_SESSION_KEY" not in env
    finally:
        for v, t in zip((sc._VAR_MAP["HERMES_SESSION_ID"], sc._VAR_MAP["HERMES_SESSION_KEY"]), tok):
            v.reset(t)
