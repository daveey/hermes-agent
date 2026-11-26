"""Real Unix transport separates release from legacy create packets and gates."""
import json
import socket
from array import array

import pytest

from hermes_cli.kanban_origin_admission import active
from tools import origin_cli_bridge as bridge, origin_cli_policy as policy
from tests.hermes_cli import test_origin_native_admission as fixtures

turn = fixtures.turn


@pytest.fixture
def launch(turn, monkeypatch):
    import model_tools
    with monkeypatch.context() as scoped:
        scoped.setattr(model_tools, "_handle_function_call", lambda *a: bridge.Launch("test"))
        captured = model_tools.handle_function_call("terminal", {}, enabled_tools=["terminal"])
    try:
        yield captured
    finally:
        captured.close()
        captured._thread.join(2)
        assert not captured._thread.is_alive()


def test_release_discriminator_dispatches_separate_policy_and_preserves_pending(launch, monkeypatch):
    called = []
    def release(self, args, gate):
        assert not active()
        called.append(args)
        gate.admit("t_target", intent_uid="exact")
        return {"ok": True, "task_id": "t_target", "intent_uid": "exact", "release_stage": "read_first_pending"}
    monkeypatch.setattr(policy._Policy, "release", release)
    result = bridge.request(launch.locator, {"task_id": "t_target"}, operation="release")
    assert result["release_stage"] == "read_first_pending" and result["ok"]
    assert called == [{"task_id": "t_target"}]
    assert launch.retire() == [{"outcome": "read_first_pending", "task_id": "t_target", "finished": True,
                                "operation": "release", "intent_uid": "exact"}]


@pytest.mark.parametrize("operation", ["release-all", "", None, 1, {"release": True}])
def test_unknown_discriminator_is_refused_without_admission(launch, operation):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        reader.settimeout(2)
        data = json.dumps({"nonce": json.loads(launch.locator)["nonce"], "args": {}, "operation": operation}).encode()
        launch._child.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [writer.fileno()]))])
        writer.close()
        assert reader.recv(65536) == b""
    assert launch.retire() == []


def test_unknown_packet_fields_do_not_become_actor_overrides(launch):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        reader.settimeout(2)
        data = json.dumps({"nonce": json.loads(launch.locator)["nonce"], "args": {}, "operation": "release",
                           "actor": {"session": "forged"}}).encode()
        launch._child.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [writer.fileno()]))])
        writer.close()
        assert reader.recv(65536) == b""
    assert launch.retire() == []


def test_legacy_create_client_keeps_original_packet_shape(launch, monkeypatch):
    seen = []
    encode = bridge._encode
    def capture(value):
        if "nonce" in value:
            seen.append(value)
        return encode(value)
    monkeypatch.setattr(bridge, "_encode", capture)
    bridge.request(launch.locator, {"title": "legacy"})
    assert len(seen) == 1 and set(seen[0]) == {"nonce", "args"}


def test_release_terminal_negative_is_preserved_not_unknown(launch, monkeypatch):
    def release(self, args, gate):
        gate.admit("t_target", intent_uid="exact")
        return {"ok": False, "task_id": "t_target", "intent_uid": "exact", "release_stage": "expired"}
    monkeypatch.setattr(policy._Policy, "release", release)
    result = bridge.request(launch.locator, {}, operation="release")
    assert result == {"ok": False, "task_id": "t_target", "intent_uid": "exact", "release_stage": "expired"}
    assert launch.retire()[0]["outcome"] == "expired"


def test_release_exception_after_admission_is_unknown_not_created(launch, monkeypatch):
    def release(self, args, gate):
        gate.admit("t_target", intent_uid="exact")
        raise OSError("private detail")
    monkeypatch.setattr(policy._Policy, "release", release)
    result = bridge.request(launch.locator, {}, operation="release")
    assert result["outcome"] == "outcome_unknown" and not result["ok"]
    assert "private detail" not in str(result)
    assert launch.retire()[0]["operation"] == "release"
