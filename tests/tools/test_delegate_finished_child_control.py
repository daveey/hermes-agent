"""delegate_task steer/stop/list on a child that already FINISHED.

A child leaves the live registry the moment ``_run_single_child`` returns, but its result reaches the parent later:
between parent turns, and only once every sibling of its batch has finished. In that window the parent still believes
the child is working and steers it. That used to return a tool error ("No live subagent 'sa-1-…' in this
conversation's spawn tree"), the coach counted it as a Hermes failure in several sessions, and the model got no
definite answer (finished? foreign? typo?). Now an owned, recently finished child yields a non-error
``status: already_finished`` with ``steer_delivered: false``, the batch it belongs to and the still-running
siblings; ``list`` names finished children whose batch result is still held. Unknown and foreign ids stay errors.
"""

import json
import weakref
from unittest.mock import MagicMock

from tools.delegate_tool import (
    _handle_control_action,
    _register_subagent,
    _unregister_subagent,
    delegate_task,
)


class _Parent:
    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self._session_db = None


class _Child:
    def __init__(self, parent=None, accept_steer: bool = True):
        self.steered: list[str] = []
        self.accept_steer = accept_steer
        if parent is not None:
            self._delegate_parent_ref = weakref.ref(parent)

    def steer(self, text: str) -> bool:
        if not self.accept_steer:
            return False
        self.steered.append(text)
        return True


def _register(sid: str, child, **extra) -> None:
    record = {"subagent_id": sid, "parent_id": None, "depth": 0, "goal": f"goal {sid}", "model": "m",
              "started_at": 1000.0, "status": "running", "tool_count": 0, "agent": child}
    record.update(extra)
    _register_subagent(record)


def test_steer_finished_owned_child_is_not_an_error():
    parent = _Parent()
    child = _Child(parent)
    _register("sa-0-fin00001", child, delegation_id="deleg_fin_1")
    _unregister_subagent("sa-0-fin00001", agent=child)

    raw = _handle_control_action("steer", "sa-0-fin00001", "late correction", parent)
    out = json.loads(raw)
    assert "error" not in out, raw
    assert out["status"] == "already_finished"
    assert out["steer_delivered"] is False
    assert out["delegation_id"] == "deleg_fin_1"
    assert out["pending_siblings"] == []
    assert "deleg_fin_1" in out["note"]
    assert child.steered == []


def test_stop_finished_owned_child_is_not_an_error():
    parent = _Parent()
    child = _Child(parent)
    _register("sa-0-fin00002", child, delegation_id="deleg_fin_2")
    _unregister_subagent("sa-0-fin00002", agent=child)

    out = json.loads(_handle_control_action("stop", "sa-0-fin00002", None, parent))
    assert out["status"] == "already_finished"
    assert "steer_delivered" not in out
    assert "Nothing to stop" in out["note"]


def test_finished_child_names_running_batch_siblings_and_list_shows_it():
    parent = _Parent()
    done, running = _Child(parent), _Child(parent)
    _register("sa-0-fin00003", done, delegation_id="deleg_fin_3")
    _register("sa-1-fin00003", running, delegation_id="deleg_fin_3")
    _unregister_subagent("sa-0-fin00003", agent=done)
    try:
        out = json.loads(_handle_control_action("steer", "sa-0-fin00003", "x", parent))
        assert out["status"] == "already_finished"
        assert out["pending_siblings"] == ["sa-1-fin00003"]
        assert "still-running sibling" in out["note"]

        listed = json.loads(_handle_control_action("list", None, None, parent))
        assert [e["subagent_id"] for e in listed["subagents"]] == ["sa-1-fin00003"]
        held = listed["finished_awaiting_batch"]
        assert [e["subagent_id"] for e in held] == ["sa-0-fin00003"]
        assert held[0]["delegation_id"] == "deleg_fin_3"
    finally:
        _unregister_subagent("sa-1-fin00003", agent=running)

    # Once the whole batch is done, nothing is held any more.
    listed = json.loads(_handle_control_action("list", None, None, parent))
    assert "finished_awaiting_batch" not in listed


def test_finished_child_resolves_through_durable_session_after_parent_rebuild():
    old_parent = _Parent("sess-fin-rebuild")
    child = _Child(old_parent)
    _register("sa-0-fin00004", child, delegation_id="deleg_fin_4", owner_agent_session_id="sess-fin-rebuild")
    _unregister_subagent("sa-0-fin00004", agent=child)
    rebuilt = _Parent("sess-fin-rebuild")

    out = json.loads(_handle_control_action("steer", "sa-0-fin00004", "x", rebuilt))
    assert out["status"] == "already_finished"


def test_finished_foreign_child_stays_an_error():
    owner = _Parent("sess-owner")
    child = _Child(owner)
    _register("sa-0-fin00005", child, delegation_id="deleg_fin_5", owner_agent_session_id="sess-owner")
    _unregister_subagent("sa-0-fin00005", agent=child)

    out = _handle_control_action("steer", "sa-0-fin00005", "hijack", _Parent("sess-other"))
    assert "No live subagent" in out
    listed = json.loads(_handle_control_action("list", None, None, _Parent("sess-other")))
    assert "finished_awaiting_batch" not in listed


def test_unknown_id_stays_an_error():
    out = _handle_control_action("steer", "sa-9-neverexisted", "x", _Parent("sess-u"))
    assert "No live subagent" in out


def test_steer_while_child_is_finishing_is_not_an_error():
    parent = _Parent()
    child = _Child(parent)
    _register("sa-0-fin00006", child, delegation_id="deleg_fin_6", accepting_steer=False)
    try:
        raw = _handle_control_action("steer", "sa-0-fin00006", "late", parent)
        out = json.loads(raw)
        assert "error" not in out, raw
        assert out["status"] == "finishing"
        assert out["steer_delivered"] is False
        assert child.steered == []
    finally:
        _unregister_subagent("sa-0-fin00006", agent=child)


def test_model_facing_steer_after_real_child_run_returns_already_finished():
    """End to end through the real child runner and the model-facing delegate_task entry point."""
    from tools.delegate_tool import _run_single_child

    parent = _Parent("sess-fin-e2e")
    child = MagicMock()
    child._subagent_id = "sa-0-fine2e01"
    child._delegate_depth = 1
    child._delegation_id = "deleg_fin_e2e"
    child._parent_session_id = "sess-fin-e2e"
    child._delegate_parent_ref = weakref.ref(parent)
    child.model = "test-model"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child._drain_pending_steer.return_value = None
    child.run_conversation.return_value = {
        "final_response": "done", "completed": True, "interrupted": False, "api_calls": 1, "messages": [],
    }

    _run_single_child(0, "finish fast", child=child, parent_agent=parent)

    raw = delegate_task(action="steer", subagent_id="sa-0-fine2e01", message="too late", parent_agent=parent)
    out = json.loads(raw)
    assert "error" not in out, raw
    assert out["status"] == "already_finished"
    assert out["delegation_id"] == "deleg_fin_e2e"
    child.steer.assert_not_called()

    # A rebuilt parent of the same conversation resolves it too (durable session spine).
    raw = delegate_task(action="stop", subagent_id="sa-0-fine2e01", parent_agent=_Parent("sess-fin-e2e"))
    assert json.loads(raw)["status"] == "already_finished"
