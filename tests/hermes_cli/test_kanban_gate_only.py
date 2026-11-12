"""Creates naming a gate-only assignee are refused; the gate's reassignment still works. [daveey]

Five-whys t_01a1d08fbacf4432 (stranded t_01a1cdf7bbf44625): 9 cards since 10-28 were created on the mini's
``bb`` board with assignee ``bb-repair-native`` (8 by bb-dev/default workers via ``kanban_create``, 1 by the
resume requester copying its target's assignee). The dispatcher spawned the native worker at once, bb_gate
reclaimed it ("native worker lacks gate authorization") and recorded ``native_rejected``, and the card was never
admitted again. ``kanban.gate_only_assignees: {bb-repair-native: bb-queue}`` now refuses such a create at every
create surface, files kernel recovery cards under the queue, and leaves ``assign_task`` (the gate's path) alone.
Every test runs on an isolated board under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_gate_only as go

GATE = "kanban:\n  queue_assignees: [bb-queue]\n  gate_only_assignees:\n    bb-repair-native: bb-queue\n"


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    return h


def _gate(home, text=GATE):
    (home / "config.yaml").write_text(text)


def _titles(conn):
    return [t.title for t in kb.list_tasks(conn)]


def _as_worker(monkeypatch, conn, tid):
    assert kb.claim_task(conn, tid) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def test_create_task_refuses_a_gate_only_assignee_and_inserts_nothing(home):
    _gate(home)
    with kbc.connect() as conn:
        with pytest.raises(go.GateOnlyAssigneeError) as caught:
            kb.create_task(conn, title="fix BB-12", assignee="bb-repair-native", created_by="bb-dev")
        assert isinstance(caught.value, ValueError)
        assert "'bb-queue'" in str(caught.value) and "gate-only" in str(caught.value)
        assert _titles(conn) == []
        # The queue assignee (what the gate admits from) and other profiles still create.
        queued = kb.create_task(conn, title="fix BB-12", assignee="bb-queue", created_by="bb-dev")
        assert kb.get_task(conn, queued).assignee == "bb-queue"
        assert kb.create_task(conn, title="other", assignee="default", created_by="bb-dev")


def test_without_the_config_the_same_create_is_unchanged(home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix BB-12", assignee="bb-repair-native", created_by="bb-dev")
        assert kb.get_task(conn, tid).assignee == "bb-repair-native"


def test_worker_kanban_create_tool_is_refused_naming_the_queue(home, monkeypatch):
    _gate(home)
    with kbc.connect() as conn:
        me = kb.create_task(conn, title="bb-dev worker card", assignee="default", created_by="default")
        _as_worker(monkeypatch, conn, me)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "BB-12 native repair", "body": "repair it",
                                        "assignee": "bb-repair-native", "requires": [], "parents": [me]}))
    assert "error" in out or out.get("ok") is False, out
    assert "gate-only" in json.dumps(out) and "bb-queue" in json.dumps(out)
    with kbc.connect() as conn:
        assert _titles(conn) == ["bb-dev worker card"]


def test_cli_create_is_refused(home, monkeypatch):
    _gate(home)
    from hermes_cli import kanban as cli
    out = cli.run_slash("create 'BB-12 native repair' --assignee bb-repair-native --json")
    assert "gate-only" in out and "bb-queue" in out, out
    with kbc.connect() as conn:
        assert _titles(conn) == []


def test_gate_reassignment_onto_the_gate_only_profile_still_works(home):
    _gate(home)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix BB-12", assignee="bb-queue", created_by="bb-dev")
        assert kb.assign_task(conn, tid, "bb-repair-native")  # bb_gate_assignment.mutate's call
        assert kb.get_task(conn, tid).assignee == "bb-repair-native"


def test_resume_requester_files_a_gate_only_targets_recovery_under_the_queue(home):
    from tests.hermes_cli import test_kanban_resume_requester as rrt
    from hermes_cli import kanban_resume_requester as rr
    with kbc.connect() as conn:
        target = kb.create_task(conn, title="native repair", assignee="bb-repair-native", created_by="bb-gate")
        rrt._exhaust(conn, target)
        _gate(home)  # declared after the card exists (the 10-28..11-12 strays predate it)
        rrt._tick(conn)
        (rec,) = rrt._recovery_cards(conn, target)
        assert kb.get_task(conn, rec).assignee == "bb-queue"


def test_goal_recovery_never_picks_a_gate_only_profile(home):
    from hermes_cli.kanban_goal_recovery import recovery_assignee
    prof = home / "profiles" / "bb-repair-native"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("toolsets: [terminal, file, kanban]\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="native repair", assignee="bb-repair-native", created_by="bb-gate")
        task = kb.get_task(conn, tid)
    assert recovery_assignee(task) == "bb-repair-native"  # capable profile: picked without the config
    _gate(home)
    assert recovery_assignee(task) == "default"


def test_config_shapes(home, monkeypatch, tmp_path):
    _gate(home, "kanban:\n  gate_only_assignees: [bb-repair-native]\n")  # a list names no queue: ignored
    assert go.gate_only_assignees() == {}
    # Malformed entries first: each must be skipped, not abort the read of the valid one after it. A dict
    # straight from the loader, since YAML config loading itself refuses some of these shapes.
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"gate_only_assignees": {
        1: "q", "x": 3, "y": " ", "": "q", " bb-repair-native ": " bb-queue "}}})
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path / "no-board-config")
    assert go.gate_only_assignees() == {"bb-repair-native": "bb-queue"}
    assert go.create_assignee("bb-repair-native") == "bb-queue"
    assert go.create_assignee("default") == "default"
    assert go.create_assignee(None) is None
    go.refuse_create(None)
    go.refuse_create("")
    with pytest.raises(go.GateOnlyAssigneeError):
        go.refuse_create(" bb-repair-native ")


def test_board_home_config_declares_it_too(home, monkeypatch, tmp_path):
    board_home = tmp_path / "board-home"
    board_home.mkdir()
    (board_home / "config.yaml").write_text(GATE)
    monkeypatch.setattr(kb, "kanban_home", lambda: board_home)
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", dict)
    assert go.gate_only_assignees() == {"bb-repair-native": "bb-queue"}
