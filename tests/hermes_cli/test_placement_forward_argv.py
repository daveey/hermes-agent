"""Foreign placement origins retain qualified authority at the CLI boundary."""
import pytest

from hermes_cli import kanban_placement as kp


@pytest.fixture
def transport(monkeypatch):
    calls = []
    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: True)
    monkeypatch.setattr(kp, "remote_state", lambda rid: "queued")
    monkeypatch.setattr(kp, "hub_runner", lambda argv: calls.append(argv) or (0, "", ""))
    return calls


def send(**overrides):
    kwargs = {"target": "hub", "self_gw": "worker-7", "origin_session": "chat-one",
              "origin_profile": "default", "origin_gateway": "macbook", "run_uid": "run-one",
              "creator": {"gateway": "hub", "profile": "default", "board": "default", "task_id": "t_parent"},
              "cfg": {"creator_session": "worker-session"}}
    return kp.create_remote({"title": "Build", "assignee": "default"}, **{**kwargs, **overrides})


def test_foreign_retry_rechecks_full_envelope_at_real_cli_boundary(transport):
    assert send()["ok"]
    argv = transport[0]
    for flag, value in {"origin-gateway": "macbook", "origin-session": "chat-one",
                        "origin-profile": "default", "creator-gateway": "hub", "creator-profile": "default",
                        "creator-board": "default", "creator-task": "t_parent", "creator-run": "run-one"}.items():
        assert argv[argv.index("--" + flag) + 1] == value


@pytest.mark.parametrize("override", [{"creator": None}, {"run_uid": None}, {"origin_session": None},
                                      {"creator": {"gateway": "hub"}}])
def test_missing_foreign_authority_is_fatal_without_send(transport, override):
    result = send(**override)
    assert not result["ok"] and result["fatal"]
    assert not transport


def test_old_courier_refusal_cannot_fall_back_to_local(transport, monkeypatch):
    monkeypatch.setattr(kp, "hub_runner", lambda argv: (2, "", "unknown --origin-gateway"))
    result = send()
    assert not result["ok"] and result["fatal"]


def test_same_gateway_retry_keeps_existing_behavior(transport):
    assert send(origin_gateway="worker-7")["retry"]
    assert not transport
