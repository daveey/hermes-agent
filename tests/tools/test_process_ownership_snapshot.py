"""Native scope, real local pipe spawn, and non-consuming ownership readback."""
import dataclasses
import sys
from types import SimpleNamespace

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.turn_context import _collect_pre_llm_call_context
from gateway.session_context import scoped_current_session_id
from tools.process_registry import ProcessRegistry


def capture(agent, text="Repair example app"):
    rows = [{"role": "user", "content": text}]
    _collect_pre_llm_call_context(agent, effective_task_id="task", turn_id="turn",
        original_user_message=text, messages=rows, conversation_history=[], current_turn_user_idx=0)
    return rows[0]["message_uid"]


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **k: [])
    agent = SimpleNamespace(session_id="origin", model="example", _current_turn_id="native-turn")
    registry = ProcessRegistry()
    with bind_subagent_parent(agent), scoped_current_session_id("origin"):
        uid = capture(agent)
        process = registry.spawn_local(
            f"exec {sys.executable} -c 'import threading; threading.Event().wait(60)'", session_key="origin")
        process.notify_on_complete = True
        try:
            yield registry, process, agent, uid
        finally:
            process.process.kill()
            process.process.wait(timeout=5)
            process._completion_event.wait(5)


def test_native_spawn_freezes_canonical_identity(native, tmp_path):
    registry, process, agent, uid = native
    snapshot = registry.ownership_snapshot(process.id)
    assert snapshot is not None
    assert (snapshot.home, snapshot.session_id, snapshot.message_uid, snapshot.content) == (
        str(tmp_path.resolve()), "origin", uid, "Repair example app")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.content = "forged"
    capture(agent, "Stop repairing")
    assert registry.ownership_snapshot(process.id) == snapshot


def test_snapshot_exact_bounded_and_nonconsuming(native, monkeypatch):
    registry, process, _, _ = native
    for name in ("get", "list_sessions", "drain_notifications", "_write_checkpoint"):
        monkeypatch.setattr(registry, name, lambda *a, **k: pytest.fail("mutating read"))
    assert registry.ownership_snapshot(process.id) is not None
    assert registry.ownership_snapshot(process.id[:9]) is None
    with registry._lock:
        assert registry.ownership_snapshot(process.id) is None
    # Undo before fixture teardown uses the normal lifecycle.
    monkeypatch.undo()


@pytest.mark.parametrize("field,value", [("detached", True), ("pid_scope", "sandbox"),
    ("persist_on_release", True), ("notify_on_complete", False), ("host_start_time", -1),
    ("exited", True), ("_reader_finish_requested", None)])
def test_unsupported_state_rejected(native, field, value):
    registry, process, _, _ = native
    previous = getattr(process, field)
    try:
        if field == "_reader_finish_requested":
            previous.set()
        else:
            setattr(process, field, value)
        assert registry.ownership_snapshot(process.id) is None
    finally:
        if field == "_reader_finish_requested":
            previous.clear()
        else:
            setattr(process, field, previous)


def test_handoff_permanently_invalidates_binding(native):
    registry, process, _, _ = native
    assert registry.transfer_ownership(process.id, from_owner="", to_owner="other",
        to_task_id="other", to_session_key="origin") is process
    assert registry.ownership_snapshot(process.id) is None


@pytest.mark.parametrize("change", ["unbound", "new-turn", "steered", "rotated"])
def test_new_spawn_requires_current_native_scope(native, change):
    registry, _, agent, _ = native
    if change == "unbound":
        agent._process_spawn_scope = None
    elif change == "new-turn":
        agent._current_turn_id = "new-native-turn"
    elif change == "steered":
        agent._turn_user_intervened = True
    else:
        agent.session_id = "rotated"
    p = registry.spawn_local(f"exec {sys.executable} -c 'import threading; threading.Event().wait(60)'",
                             session_key="origin")
    p.notify_on_complete = True
    try:
        assert registry.ownership_snapshot(p.id) is None
    finally:
        p.process.kill()
        p.process.wait(timeout=5)
        p._completion_event.wait(5)


def test_finished_process_is_not_retained_ownership(native):
    registry, process, _, _ = native
    process.process.kill()
    process.process.wait(timeout=5)
    assert process._completion_event.wait(5)
    assert registry.ownership_snapshot(process.id) is None
    assert process.id not in registry._ownership_bindings
