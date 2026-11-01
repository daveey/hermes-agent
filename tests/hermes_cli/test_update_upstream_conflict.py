"""The conflict reporter must not depend on the updater's pre-update ``sys.modules``.

``hermes update`` imports ``hermes_cli.config_effective`` at startup, then moves the checkout. When
``report()`` ran in-process, the newly pulled ``kanban_capabilities`` asked that *old* in-memory module
for a name it did not have yet, and validate_assignment surfaced the ImportError as "Assignee 'default'
lacks required capabilities", so no repair owner was filed (defect kanban-c50ee42be6).
"""
import sys
import types
from pathlib import Path

from hermes_cli.config import save_config
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import update_upstream_conflict as uuc


def _stale_config_effective(monkeypatch):
    # The pre-update module: present, but without ``load_profile_config_effective``.
    stale = types.ModuleType("hermes_cli.config_effective")
    stale.__file__ = "<stale pre-update module>"
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", stale)


def test_report_files_owner_despite_stale_in_process_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    save_config({"platform_toolsets": {"cli": ["terminal", "file"]}, "terminal": {"backend": "local"}})
    _stale_config_effective(monkeypatch)
    cwd = tmp_path / "checkout"
    cwd.mkdir()

    task_id = uuc.report(cwd, "a" * 40, ["a.txt"])

    assert task_id and task_id.startswith("t_")
    with connect_closing() as conn:
        rows = conn.execute("SELECT id, idempotency_key, body FROM tasks").fetchall()
    assert [r[0] for r in rows] == [task_id]
    assert rows[0][1].startswith("upstream-merge-conflict:")
    assert "a" * 40 in rows[0][2] and "a.txt" in rows[0][2]

    # A second occurrence refreshes the same owner instead of creating another.
    assert uuc.report(cwd, "b" * 40, ["a.txt"]) == task_id
    with connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        comments = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)).fetchall()]
    assert any("b" * 40 in c for c in comments)


def test_report_failure_is_best_effort_and_logged(tmp_path, monkeypatch, capsys, caplog):
    # A child that cannot file (unwritable board path) returns None, warns, and keeps its stderr tail.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(blocker / "board.db"))
    with caplog.at_level("WARNING", logger=uuc.__name__):
        assert uuc.report(tmp_path, "c" * 40, ["a.txt"]) is None
    assert "could not file upstream merge conflict card" in capsys.readouterr().out
    assert any("Could not file upstream merge conflict card" in r.getMessage() for r in caplog.records)


def test_conflict_without_owner_prints_not_filed(monkeypatch, tmp_path, capsys):
    from hermes_cli import update_cmd_git as ucg

    monkeypatch.setattr(uuc, "report", lambda *a, **k: None)
    calls = iter([
        types.SimpleNamespace(returncode=1, stdout="", stderr=""),
        types.SimpleNamespace(returncode=1, stdout="tree\na.txt\n", stderr=""),
    ])
    monkeypatch.setattr(ucg.subprocess, "run", lambda *a, **k: next(calls))
    monkeypatch.setattr(ucg, "_git_stdout", lambda *a, **k: "head")
    assert ucg._synthesize_upstream_merge(["git"], Path(tmp_path), "u" * 40) is None
    assert "Repair owner: NOT FILED" in capsys.readouterr().out
