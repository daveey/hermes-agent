"""Operator-only capability corrections are validated, durable and audited."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_edit_operator_fields as helpers

kanban_home = helpers.kanban_home


def test_edit_requires_cli_readback_and_audit(kanban_home, capsys):
    tid = helpers._new(requires=["terminal", "host_repo"])
    assert helpers._main(["edit", tid, "--requires", "terminal,file,git"]) == 0
    assert "requires:" in capsys.readouterr().out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).requires == ["terminal", "file", "git"]
        event = [e for e in kb.list_events(conn, tid) if e.kind == "edited"][-1]
        assert event.payload["changes"]["requires"] == {
            "old": ["terminal", "host_repo"], "new": ["terminal", "file", "git"]}
    assert helpers._main(["edit", tid, "--requires", "terminal,file,git"]) == 0
    assert len(helpers._edited_events(tid)) == 1


@pytest.mark.parametrize("variable", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_worker_cannot_edit_requires(kanban_home, monkeypatch, variable, capsys):
    tid = helpers._new(requires=["host_repo"])
    monkeypatch.setenv(variable, tid)
    assert helpers._main(["edit", tid, "--requires", "terminal"]) == 2
    assert "orchestrator-only" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).requires == ["host_repo"]
    assert helpers._edited_events(tid) == []


@pytest.mark.parametrize("value", ["bogus", "terminal,,file", "terminal,ssh:"])
def test_edit_requires_invalid_values_leave_card_unchanged(kanban_home, value):
    tid = helpers._new(requires=["host_repo"])
    assert helpers._main(["edit", tid, "--requires", value]) == 2
    assert helpers._edited_events(tid) == []


def test_edit_requires_can_explicitly_clear(kanban_home):
    tid = helpers._new(requires=["host_repo"])
    assert helpers._main(["edit", tid, "--requires", ""]) == 0
    with kbc.connect() as conn:
        assert json.loads(conn.execute('SELECT requires FROM tasks WHERE id=?', (tid,)).fetchone()[0]) == []


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_requires_edit_preserves_nonidle_or_closed_cards(kanban_home, status):
    tid = helpers._new(requires=["host_repo"])
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
    assert helpers._main(["edit", tid, "--requires", "terminal"]) == 2
    assert helpers._edited_events(tid) == []


def test_requires_edit_retains_workspace_needs(kanban_home, tmp_path):
    tid = helpers._new(workspace_kind="dir", workspace_path=str(tmp_path), requires=["host_repo"])
    assert helpers._main(["edit", tid, "--requires", ""]) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).requires == ["terminal", "file", "host_repo"]


def test_requires_edit_refuses_combined_flags(kanban_home):
    tid = helpers._new(requires=["host_repo"])
    assert helpers._main(["edit", tid, "--requires", "terminal", "--title", "replacement"]) == 2
    assert helpers._edited_events(tid) == []


def test_requires_edit_unknown_card(kanban_home, capsys):
    assert helpers._main(["edit", "t_missing", "--requires", "terminal"]) == 1
    assert "unknown id" in capsys.readouterr().err


def test_requires_edit_force_is_not_an_escape(kanban_home):
    tid = helpers._new(requires=["host_repo"])
    assert helpers._main(["edit", tid, "--requires", "terminal", "--force"]) == 2
    assert helpers._edited_events(tid) == []


def test_requires_edit_retains_skill_requirements(kanban_home, monkeypatch):
    from hermes_cli import kanban_capabilities as capabilities
    tid = helpers._new(requires=["host_repo"])
    monkeypatch.setattr(capabilities, "skill_requirements", lambda skills, profile: ["host_repo"])
    assert helpers._main(["edit", tid, "--requires", "terminal"]) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).requires == ["terminal", "host_repo"]


@pytest.mark.parametrize("field", ["claim_lock", "lease_holder"])
def test_requires_edit_rejects_ready_card_with_custodian(kanban_home, field):
    tid = helpers._new(requires=["host_repo"])
    with kbc.connect() as conn:
        # Unsynced fixtures lack the server lease mirror; install just that column.
        if field == "lease_holder":
            conn.execute("ALTER TABLE tasks ADD COLUMN lease_holder TEXT")
        conn.execute(f"UPDATE tasks SET {field}='custodian' WHERE id=?", (tid,))
        conn.commit()
    assert helpers._main(["edit", tid, "--requires", "terminal"]) == 2
    assert helpers._edited_events(tid) == []
