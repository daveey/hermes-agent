"""Restriction lineage: authenticated root registration, descendants, and no authority from the token."""
import gc
from types import SimpleNamespace

import pytest

from agent import kanban_delegation_policy as policy
from tests.tools import test_kanban_delegation_no_pin as pins

worker = pins.worker
effects = pins.effects
interactive = pins.interactive


class _Child:
    """Weakref-able stand-in for a built child agent."""


@pytest.fixture
def root(worker):
    agent = worker[0]
    policy.register_root(agent, agent._test_original_worker_custody)
    agent.model_routing = {"requested_class": "fast"}
    return agent


def test_root_registers_once_and_classifies(root):
    assert policy.origin_for(root) is not None
    with pytest.raises(policy.RestrictionError):
        policy.register_root(root, root._test_original_worker_custody)


def test_registration_requires_the_exact_bound_capability(worker):
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    agent, store = worker[0], worker[1]
    other = _agent_with_db(store, session_id=agent.session_id, platform="kanban")
    with pytest.raises(policy.RestrictionError):
        policy.register_root(other, agent._test_original_worker_custody)  # never bound to `other`
    with pytest.raises(policy.RestrictionError):
        policy.register_root(agent, SimpleNamespace(_handoff=None))  # not a WorkerCustody
    with pytest.raises(policy.RestrictionError):
        policy.register_root(agent, None)


def test_unregistered_interactive_parent_is_unclassified(interactive):
    assert policy.origin_for(interactive) is None


def test_bound_native_agent_without_registration_fails_closed(worker):
    with pytest.raises(policy.RestrictionError):
        policy.origin_for(worker[0])


def test_child_lineage_and_grandchild_restriction(root, effects, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 4)
    token = policy.origin_for(root)
    child, grandchild = _Child(), _Child()
    policy.register_child(root, child, token)
    assert policy.origin_for(child) is token
    child._delegate_depth = 1
    policy.register_child(child, grandchild, policy.origin_for(child))
    assert policy.origin_for(grandchild) is token
    grandchild._delegate_depth = 2
    child.model_routing = {"requested_class": "standard"}
    result = pins._caller_facade(child, [{"goal": "grandchild pin attempt", "model": "gpt-5"}])
    assert pins._refused(result) and "depth limit" not in result
    assert effects.calls == []
    assert pins.ADMITTED in pins._caller_facade(child, [{"goal": "grandchild class work"}])
    assert effects.routes[0].creds["model"] == "class:standard"


def test_token_for_an_unrelated_parent_refuses(root, interactive):
    token = policy.origin_for(root)
    with pytest.raises(policy.RestrictionError):
        policy.register_child(interactive, _Child(), token)
    with pytest.raises(policy.RestrictionError):
        policy.register_child(root, _Child(), object())
    assert policy.origin_for(interactive) is None


def test_registered_descendant_gains_no_native_creator_authority(root):
    child = _Child()
    policy.register_child(root, child, policy.origin_for(root))
    assert getattr(child, "_native_origin_worker_custody", None) is None
    capability = root._native_origin_worker_custody
    assert capability.validated(None) is None  # no live facade turn: the token never grants a lease


def test_registration_is_weak(root):
    child = _Child()
    policy.register_child(root, child, policy.origin_for(root))
    ident = id(child)
    assert policy._registered(ident) is True
    del child
    gc.collect()
    assert policy._registered(ident) is False
