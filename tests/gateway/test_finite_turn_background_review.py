"""A finite turn (``hermes -z`` / ``chat -q``) never forks the post-turn skill/memory review, as the
classic single-query process did (``skip_background_review=single_query_mode``) — including
``-z --resume`` of an existing ``cli`` session, whose source stays ``cli`` and whose agent may be the
gateway's cached one. The override is per turn: the next interactive turn on the same agent keeps its
review, and the session's source/policy are never changed.

These drive the real in-process turn body (``TurnRunner._run_sync_scoped``) up to the agent run, whose
stand-in calls the real turn finalizer (``agent.turn_finalizer._mirror_turn_to_memory``)."""
import types

import pytest

from gateway.session_finite import finite_turn_scope

LAUNCH = {"model": "m", "provider": "custom", "base_url": "http://127.0.0.1:9/v1"}


class _TurnDone(Exception):
    pass


class _Agent:
    """The attributes the turn body and the finalizer touch; records each forked review."""

    def __init__(self, skip_background_review=False, **_kwargs):
        self.skip_background_review = skip_background_review
        self.reviews = []

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass

    def _spawn_background_review(self, **kwargs):
        self.reviews.append(kwargs)


def _policy(monkeypatch, tmp_path, source):
    from gateway import session_policy
    policy = session_policy.build_policy({"cwd": str(tmp_path), "source": source, **LAUNCH}, {})
    assert policy.source == source
    monkeypatch.setattr(session_policy, "policy_for_source", lambda _runner, _source: policy)
    return policy


def _run_turn(agent, *, reused, finite):
    """One gateway turn on *agent*; returns ``skip_background_review`` as the finalizer saw it."""
    from agent.turn_finalizer import _mirror_turn_to_memory
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner

    seen = {}
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = types.SimpleNamespace(
        _provider_routing={}, _peek_session_state=lambda key: None,
        session_authority=types.SimpleNamespace(profile_id="p"),
        _resolve_session_agent_runtime=lambda **kw: ("m", {"provider": "custom"}),
        _resolve_turn_agent_config=lambda message, model, runtime: {"model": model, "runtime": runtime})
    turn._ctx = types.SimpleNamespace(
        source=types.SimpleNamespace(platform=Platform.LOCAL, chat_id="sid"), session_key="k", session_id="sid",
        user_config={}, message="hi", inbound_message_id=None, enabled_toolsets=[], mute_notification_reply=False)
    turn._combined_ephemeral_prompt = lambda: None
    turn._setup_stream_consumer = lambda platform_key: (None, None, None, False)
    turn._resolve_turn_agent = lambda *_a: (agent, reused)
    turn._wire_turn_agent_callbacks = lambda *_a: None
    turn._load_turn_history = lambda _agent, _reused: ([], None, [])
    turn._prepare_turn_message = lambda _history: (None, None)

    def run(run_agent, *_a):
        seen["skip"] = run_agent.skip_background_review
        _mirror_turn_to_memory(run_agent, original_user_message="hi", final_response="ok", interrupted=False,
                               messages=[{"role": "assistant", "content": "ok"}], review_memory=True,
                               review_skills=True, vetoed=False)
        return {"final_response": "ok", "messages": []}

    turn._run_conversation_with_approval = run

    def finish(*_a):
        raise _TurnDone()

    turn._finish_stream_consumer = finish
    with finite_turn_scope(finite), pytest.raises(_TurnDone):
        turn._run_sync_scoped()
    return seen["skip"]


def _fresh_agent(monkeypatch, tmp_path):
    """The agent ``GatewaySessionAgentMixin._build_fresh_agent`` builds for this session (no cache hit)."""
    from gateway.session_execution import GatewaySessionAgentMixin
    monkeypatch.setattr("gateway.session_worker_construct.session_prefill", lambda *_a: None)
    built = []
    builder = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
    builder._runner = types.SimpleNamespace(session_authority=types.SimpleNamespace(), _prefill_messages=None,
                                            _service_tier=None, _session_db=None, _refresh_fallback_model=lambda: None)
    src = types.SimpleNamespace(chat_id="sid", user_id="u", user_id_alt=None, user_name=None, chat_name=None,
                                chat_type="dm", thread_id=None)
    builder._ctx = types.SimpleNamespace(source=src, user_config={}, enabled_toolsets=[], disabled_toolsets=None,
                                         session_id="sid", session_key="k",
                                         AIAgent=lambda **kw: built.append(_Agent(**kw)) or built[-1])
    builder._build_fresh_agent({"model": "m", "runtime": {}}, "local", None, 1, None, {}, False)
    return built[0]


def test_z_resume_on_cached_cli_agent_skips_review_then_interactive_turn_keeps_it(monkeypatch, tmp_path):
    policy = _policy(monkeypatch, tmp_path, "cli")
    cached = _Agent()  # the gateway's cached agent of an interactive CLI session

    assert _run_turn(cached, reused=True, finite=True) is True
    assert cached.reviews == []
    assert cached.skip_background_review is False  # restored: nothing persisted on the shared agent
    assert policy.source == "cli"  # session identity untouched

    assert _run_turn(cached, reused=True, finite=False) is False
    assert len(cached.reviews) == 1


def test_z_resume_on_fresh_cli_agent_skips_review_then_interactive_turn_keeps_it(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, "cli")
    fresh = _fresh_agent(monkeypatch, tmp_path)
    assert fresh.skip_background_review is False  # a cli session's agent is built with review on

    assert _run_turn(fresh, reused=False, finite=True) is True
    assert fresh.reviews == []
    assert fresh.skip_background_review is False

    assert _run_turn(fresh, reused=True, finite=False) is False
    assert len(fresh.reviews) == 1


def test_interactive_cli_turn_keeps_review(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, "cli")
    agent = _Agent()
    assert _run_turn(agent, reused=True, finite=False) is False
    assert len(agent.reviews) == 1


def test_oneshot_session_resumed_interactively_keeps_review(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, "oneshot")
    agent = _fresh_agent(monkeypatch, tmp_path)
    assert agent.skip_background_review is False
    assert _run_turn(agent, reused=False, finite=True) is True
    assert agent.reviews == []
    assert agent.skip_background_review is False
    assert _run_turn(agent, reused=True, finite=False) is False
    assert len(agent.reviews) == 1


def test_finite_turn_preserves_an_explicit_agent_skip(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, "cli")
    agent = _Agent(skip_background_review=True)
    assert _run_turn(agent, reused=True, finite=True) is True
    assert agent.skip_background_review is True
    assert agent.reviews == []
