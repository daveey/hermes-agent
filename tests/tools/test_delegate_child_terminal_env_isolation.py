"""delegate_task children must not change the parent's terminal env. [daveey]

Children share the parent's terminal environment object (the #16177 shared-container intent), and
that object persists a bash snapshot sourced before every command. A child's
``export HERMES_KANBAN_DB=.../runners/kanban.db`` therefore landed in the parent's shell, so the
parent's ``hermes kanban`` read the wrong board. Each child now writes its own overlay snapshot
(read-through to the parent's), and the parent snapshot never stores the delegate-child markers.
"""
from __future__ import annotations

import pytest

from agent.delegation_context import delegated_child_context

pytestmark = pytest.mark.platforms("posix")

_SCRUB = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
          "HERMES_KANBAN_CLAIM_LOCK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_DELEGATED_CHILD_OWNER",
          "PARENT_VAR", "CHILD_VAR")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in _SCRUB:
        monkeypatch.delenv(key, raising=False)
    from tools.environments.local import LocalEnvironment

    e = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    e.init_session()
    yield e
    e.cleanup()


def _out(env, cmd):
    return env.execute(cmd).get("output", "").strip()


def test_child_export_does_not_reach_parent(env):
    _out(env, "export PARENT_VAR=p")
    with delegated_child_context():
        _out(env, "export CHILD_VAR=c HERMES_KANBAN_DB=/x/boards/runners/kanban.db HERMES_KANBAN_BOARD=runners")
        # The child keeps its own exports across commands and starts from the parent's state.
        assert _out(env, 'echo "[$CHILD_VAR][$PARENT_VAR][$HERMES_KANBAN_BOARD]"') == "[c][p][runners]"
    assert _out(env, 'echo "[$CHILD_VAR][$PARENT_VAR][$HERMES_KANBAN_DB][$HERMES_KANBAN_BOARD]"') == "[][p][][]"
    # A sibling child does not see the first child's exports either.
    with delegated_child_context():
        assert _out(env, 'echo "[$CHILD_VAR][$PARENT_VAR]"') == "[][p]"


def test_nested_child_reads_through_enclosing_child(env):
    with delegated_child_context():
        _out(env, "export CHILD_VAR=outer")
        with delegated_child_context():
            assert _out(env, 'echo "[$CHILD_VAR]"') == "[outer]"
            _out(env, "export CHILD_VAR=inner")
        assert _out(env, 'echo "[$CHILD_VAR]"') == "[outer]"
    assert _out(env, 'echo "[$CHILD_VAR]"') == "[]"


def test_parent_terminal_never_carries_delegated_child_vars(env):
    with delegated_child_context():
        # The child's own commands are fenced (marker stamped by its Popen env) ...
        assert "HERMES_DELEGATED_CHILD_CONTEXT=" in _out(env, "env | grep ^HERMES_DELEGATED_CHILD_ || true")
        # ... and even an explicit export of the markers stays in the child.
        _out(env, "export HERMES_DELEGATED_CHILD_CONTEXT=/leak HERMES_DELEGATED_CHILD_OWNER=1:2")
    assert _out(env, "env | grep -c ^HERMES_DELEGATED_CHILD_ || true") == "0"
    with open(env._snapshot_path, encoding="utf-8") as fh:
        assert "HERMES_DELEGATED_CHILD_" not in fh.read()


def test_cleanup_removes_child_overlays(env, tmp_path):
    import glob

    with delegated_child_context():
        _out(env, "export CHILD_VAR=c")
    pattern = env._child_snapshot_path("*")
    assert glob.glob(pattern)
    env.cleanup()
    assert not glob.glob(pattern)
