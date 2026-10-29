"""``hermes kanban edit`` operator flags and ``hermes kanban reopen``: the supported
route for the board writes orchestrator skills used to do with raw sqlite3. [daveey]"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator as kbo


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _new(**kw) -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=kw.pop("title", "card"), **kw)


def _show(task_id: str) -> dict:
    return json.loads(kc.run_slash(f"show {task_id} --json"))


def _edited_events(task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        return [e.payload for e in kb.list_events(conn, task_id) if e.kind == "edited"]


def _main(argv: list[str]) -> int:
    """Run the real argparse + dispatch path (rc and refusals included)."""
    import argparse

    from hermes_cli.kanban_parser import build_parser

    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    build_parser(sub)
    args = root.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


# --- one test per edit flag ------------------------------------------------


def test_edit_max_retries_reads_back_in_show_json(kanban_home):
    tid = _new()
    out = kc.run_slash(f"edit {tid} --max-retries 5")
    assert "max_retries: None -> 5" in out
    assert _show(tid)["task"]["max_retries"] == 5
    assert _edited_events(tid)[-1] == {
        "fields": ["max_retries"], "changes": {"max_retries": {"old": None, "new": 5}},
    }


def test_edit_goal_mode_on_and_off(kanban_home):
    tid = _new()
    kc.run_slash(f"edit {tid} --goal-mode on")
    assert _show(tid)["task"]["goal_mode"] is True
    kc.run_slash(f"edit {tid} --goal-mode off")
    assert _show(tid)["task"]["goal_mode"] is False
    assert [e["changes"]["goal_mode"] for e in _edited_events(tid)] == [
        {"old": 0, "new": 1}, {"old": 1, "new": 0},
    ]


def test_edit_goal_max_turns(kanban_home):
    tid = _new(goal_mode=True, goal_max_turns=20)
    kc.run_slash(f"edit {tid} --goal-max-turns 40")
    assert _show(tid)["task"]["goal_max_turns"] == 40
    assert _edited_events(tid)[-1]["changes"] == {"goal_max_turns": {"old": 20, "new": 40}}


def test_edit_max_runtime_seconds(kanban_home):
    tid = _new(max_runtime_seconds=600)
    kc.run_slash(f"edit {tid} --max-runtime-seconds 7200")
    assert _show(tid)["task"]["max_runtime_seconds"] == 7200
    assert _edited_events(tid)[-1]["changes"] == {"max_runtime_seconds": {"old": 600, "new": 7200}}


def test_edit_session_id_backfills_empty(kanban_home):
    tid = _new()
    kc.run_slash(f"edit {tid} --session-id 20261029_000000_abc123")
    assert _show(tid)["task"]["session_id"] == "20261029_000000_abc123"
    assert _edited_events(tid)[-1]["changes"] == {
        "session_id": {"old": None, "new": "20261029_000000_abc123"},
    }


def test_edit_session_id_force_overwrites(kanban_home):
    tid = _new(session_id="old-sid")
    assert _main(["edit", tid, "--session-id", "new-sid", "--force"]) == 0
    assert _show(tid)["task"]["session_id"] == "new-sid"
    assert _edited_events(tid)[-1]["changes"] == {"session_id": {"old": "old-sid", "new": "new-sid"}}


def test_edit_operator_and_classic_fields_together(kanban_home):
    tid = _new(title="old")
    assert _main(["edit", tid, "--title", "new", "--max-retries", "2"]) == 0
    task = _show(tid)["task"]
    assert (task["title"], task["max_retries"]) == ("new", 2)


def test_edit_same_value_appends_no_event(kanban_home):
    tid = _new(max_retries=3)
    assert _main(["edit", tid, "--max-retries", "3"]) == 0
    assert _edited_events(tid) == []


# --- one test per edit refusal ---------------------------------------------


def test_edit_session_id_refuses_overwrite_without_force(kanban_home, capsys):
    tid = _new(session_id="old-sid")
    assert _main(["edit", tid, "--session-id", "new-sid", "--max-retries", "4"]) == 1
    assert "pass --force" in capsys.readouterr().err
    task = _show(tid)["task"]
    assert (task["session_id"], task["max_retries"]) == ("old-sid", None)  # nothing half-applied
    assert _edited_events(tid) == []


@pytest.mark.parametrize("flag", ["--max-retries", "--goal-max-turns", "--max-runtime-seconds"])
def test_edit_refuses_non_positive_counts(kanban_home, capsys, flag):
    tid = _new()
    assert _main(["edit", tid, flag, "0"]) == 1
    assert "positive integer" in capsys.readouterr().err
    assert _edited_events(tid) == []


def test_edit_refuses_unknown_task(kanban_home, capsys):
    assert _main(["edit", "t_nope", "--max-retries", "5"]) == 1
    assert "unknown id" in capsys.readouterr().err


def test_edit_refuses_force_without_session_id(kanban_home, capsys):
    tid = _new()
    assert _main(["edit", tid, "--max-retries", "5", "--force"]) == 2
    assert "--force only applies to --session-id" in capsys.readouterr().err
    assert _show(tid)["task"]["max_retries"] is None


def test_edit_goal_mode_rejects_other_values(kanban_home):
    tid = _new()
    with pytest.raises(SystemExit):
        _main(["edit", tid, "--goal-mode", "maybe"])


def test_edit_db_function_refuses_blank_session_id(kanban_home):
    tid = _new()
    with kbc.connect_closing() as conn, pytest.raises(kbo.KanbanEditRefused):
        kbo.edit_operator_fields(conn, tid, session_id="  ")


# --- reopen -----------------------------------------------------------------


def _done(**kw) -> str:
    tid = _new(**kw)
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="ok", force=True)
    return tid


def test_reopen_done_task_goes_ready_with_event(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "operator-x")
    tid = _done()
    assert _show(tid)["task"]["completed_at"]
    assert _main(["reopen", tid, "--reason", "acceptance was wrong"]) == 0
    payload = _show(tid)
    assert payload["task"]["status"] == "ready"
    assert payload["task"]["completed_at"] is None
    with kbc.connect_closing() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "reopened"]
        comments = kb.list_comments(conn, tid)
    assert len(events) == 1
    assert events[0].payload["reason"] == "acceptance was wrong"
    assert events[0].payload["actor"]
    assert events[0].payload["previous_status"] == "done"
    assert any(c.body == "REOPENED: acceptance was wrong" for c in comments)


def test_reopen_invalidates_done_descendants(kanban_home):
    parent = _done(title="parent")
    child = _new(title="child")
    with kbc.connect_closing() as conn:
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.recompute_ready(conn)
        assert kb.complete_task(conn, child, result="ok", force=True)
    assert _main(["reopen", parent, "--reason", "redo"]) == 0
    assert _show(parent)["task"]["status"] == "ready"
    assert _show(child)["task"]["status"] == "todo"


@pytest.mark.parametrize("status", ["ready", "blocked", "review", "archived"])
def test_reopen_refuses_non_done(kanban_home, capsys, status):
    tid = _new()
    with kbc.connect_closing() as conn:
        if status == "blocked":
            assert kb.block_task(conn, tid, reason="x")
        elif status == "review":
            assert kb.request_review(conn, tid, summary="s", force=True)
        elif status == "archived":
            assert kb.archive_task(conn, tid)
    before = _show(tid)["task"]["status"]
    assert _main(["reopen", tid, "--reason", "nope"]) == 1
    assert "not 'done'" in capsys.readouterr().err
    assert _show(tid)["task"]["status"] == before
    with kbc.connect_closing() as conn:
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "reopened"]


def test_reopen_refuses_unknown_task(kanban_home, capsys):
    assert _main(["reopen", "t_nope", "--reason", "x"]) == 1
    assert "unknown id" in capsys.readouterr().err


def test_reopen_requires_reason(kanban_home, capsys):
    tid = _done()
    with pytest.raises(SystemExit):
        _main(["reopen", tid])
    assert _main(["reopen", tid, "--reason", "   "]) == 2
    assert _show(tid)["task"]["status"] == "done"


# --- completion contract -------------------------------------------------------
# A finished card whose bound PR can never be read (its repo was suspended) used to
# park on a human "approve closing this card" ask: nothing could re-point the gate.


_GONE_PR = "https://github.com/gone-owner/repo/pull/336"


def _contract_events(task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        return [e.payload for e in kb.list_events(conn, task_id) if e.kind == "contract_changed"]


def _contract(task_id: str) -> str:
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _gate_reads_a_vanished_repo(monkeypatch) -> list:
    """The real gate with only its GitHub read replaced: the bound repo no longer resolves."""
    from hermes_cli import kanban_pr_acceptance as kpa

    calls: list = []

    def gone(contract, published_pr, assignee=None):
        calls.append(contract)
        return {"ok": False, "classification": "auth", "head_sha": None, "pr_url": contract,
                "checks": [], "recovery": "x", "detail": "repository not found on graphql"}

    monkeypatch.setattr(kpa, "collect_acceptance", gone)
    return calls


def test_contract_repoint_lets_a_card_bound_to_a_vanished_repo_complete(kanban_home, monkeypatch):
    calls = _gate_reads_a_vanished_repo(monkeypatch)
    tid = _new(completion_contract=_GONE_PR)
    with kbc.connect_closing() as conn:
        assert not kb.complete_task(conn, tid, summary="AC-8 passed live", force=True)
    assert calls == [_GONE_PR]
    rc = _main(["edit", tid, "--completion-contract", "local-only",
                "--reason", "repo suspended; the merge commit is on the new home"])
    assert rc == 0
    assert _contract(tid) == "local-only"
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary="AC-8 passed live", force=True)
    assert calls == [_GONE_PR]  # local-only: the gate is not consulted again
    assert _show(tid)["task"]["status"] == "done"


def test_contract_repoint_records_event_and_comment(kanban_home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_PROFILE", "operator-x")
    tid = _new(completion_contract=_GONE_PR)
    assert _main(["edit", tid, "--completion-contract", "new-owner/repo", "--reason", "repo moved"]) == 0
    assert f"completion_contract: '{_GONE_PR}' -> 'new-owner/repo'" in capsys.readouterr().out
    assert _contract(tid) == "new-owner/repo"
    events = _contract_events(tid)
    assert len(events) == 1
    assert events[0]["from"] == _GONE_PR and events[0]["to"] == "new-owner/repo"
    assert events[0]["reason"] == "repo moved" and events[0]["actor"]
    with kbc.connect_closing() as conn:
        bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert f"CONTRACT CHANGED: {_GONE_PR} -> new-owner/repo: repo moved" in bodies


def test_contract_repoint_works_on_a_blocked_card(kanban_home):
    tid = _new(completion_contract=_GONE_PR)
    with kbc.connect_closing() as conn:
        assert kb.block_task(conn, tid, reason="gate can't read the PR")
    assert _main(["edit", tid, "--completion-contract", "local-only", "--reason", "repo suspended"]) == 0
    assert _contract(tid) == "local-only"
    assert _show(tid)["task"]["status"] == "blocked"  # re-pointing never unblocks by itself


def test_contract_repoint_same_value_appends_nothing(kanban_home, capsys):
    tid = _new(completion_contract="owner/repo")
    assert _main(["edit", tid, "--completion-contract", "owner/repo", "--reason", "noop"]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert _contract_events(tid) == []


@pytest.mark.parametrize("argv_tail, needle", [
    (["--completion-contract", "local-only"], "requires --reason"),
    (["--completion-contract", "local-only", "--reason", "   "], "requires --reason"),
    (["--completion-contract", "not a contract", "--reason", "x"], "completion_contract must be"),
    (["--completion-contract", "local-only", "--reason", "x", "--title", "renamed"], "can't be combined"),
    (["--completion-contract", "local-only", "--reason", "x", "--max-retries", "3"], "can't be combined"),
    (["--reason", "x", "--title", "renamed"], "--reason only applies"),
])
def test_contract_repoint_refusals_change_nothing(kanban_home, capsys, argv_tail, needle):
    tid = _new(title="card", completion_contract=_GONE_PR)
    assert _main(["edit", tid, *argv_tail]) == 2
    assert needle in capsys.readouterr().err
    assert _contract(tid) == _GONE_PR
    assert _show(tid)["task"]["title"] == "card"
    assert _contract_events(tid) == []


def test_contract_repoint_is_refused_inside_a_kanban_worker(kanban_home, monkeypatch, capsys):
    tid = _new(completion_contract=_GONE_PR)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert _main(["edit", tid, "--completion-contract", "local-only", "--reason", "let me out"]) == 2
    assert "orchestrator-only" in capsys.readouterr().err
    assert _contract(tid) == _GONE_PR
    assert _contract_events(tid) == []


@pytest.mark.parametrize("status", ["done", "archived"])
def test_contract_repoint_refuses_closed_cards(kanban_home, capsys, status):
    tid = _done() if status == "done" else _new()
    if status == "archived":
        with kbc.connect_closing() as conn:
            assert kb.archive_task(conn, tid)
    assert _main(["edit", tid, "--completion-contract", "owner/repo", "--reason", "x"]) == 2
    assert "history" in capsys.readouterr().err
    assert _contract(tid) == "local-only"


def test_contract_repoint_refuses_unknown_task(kanban_home, capsys):
    assert _main(["edit", "t_nope", "--completion-contract", "local-only", "--reason", "x"]) == 1
    assert "unknown id" in capsys.readouterr().err
