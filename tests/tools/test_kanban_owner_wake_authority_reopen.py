"""Exercise authority recovery through the public tool with default -> bb custody."""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_containment_lifecycle as containment
from tests.tools import test_kanban_owner_wake_requeue as wake
from tools import kanban_tools as kt

board = wake.board


def _fixture(monkeypatch, *, cross=True):
    slug = "bb" if cross else "default"
    kb.create_board(slug)
    with kbc.connect_closing(db_path=kb.board_own_db_path(slug)) as conn:
        relay, consumer, authority, target, spec = containment.setup(conn)
        target["board"] = slug
        spec["authority"]["board"] = slug
        episode = containment.contain(conn, relay, target, spec)["episode"]
        conn.execute("UPDATE tasks SET status='done', completed_at=1, current_run_id=NULL WHERE id=?", (authority,))
        other = kb.create_task(conn, title="Unrelated done authority", assignee="default")
        conn.execute("UPDATE tasks SET status='done', completed_at=1 WHERE id=?", (other,))
        conn.commit()
    with kbc.connect_closing() as own:
        supervisor = wake._supervisor(own, kb, [wake._line(f"{slug}/{relay}")])
        wake._as_worker(monkeypatch, own, kb, supervisor)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.board_own_db_path("default")))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    return slug, relay, consumer, authority, other, supervisor, episode


def _call(slug, relay, authority):
    return json.loads(kt._handle_unblock({"task_id": relay, "board": slug, "reopen_authority": authority}))


@pytest.mark.parametrize("cross", [True, False])
def test_public_tool_reopens_only_authority_and_preserves_episode(board, monkeypatch, cross):
    slug, relay, consumer, authority, _, _, episode = _fixture(monkeypatch, cross=cross)
    out = _call(slug, relay, authority)
    assert out.get("ok") is True, out
    assert out["via"] == "owner-wake-authority-reopen"
    with kbc.connect_closing(db_path=kb.board_own_db_path(slug)) as conn:
        assert kb.get_task(conn, authority).status == "ready"
        assert kb.get_task(conn, relay).containment_episode == episode
        assert kb.get_task(conn, relay).status == "triage"
        assert kb.get_task(conn, consumer).status == "todo"
        assert conn.execute("SELECT count(*) FROM task_links WHERE parent_id=? AND child_id=?",
                            (relay, consumer)).fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='containment_released'", (relay,)).fetchone()


@pytest.mark.parametrize("case", ["unrelated", "wrong_board", "wrong_target", "non_owner", "stale_run", "ended_run", "dead_run", "child", "direct_resume", "shell"])
def test_public_boundaries_deny_without_changing_authority(board, monkeypatch, case):
    slug, relay, _, authority, other, supervisor, episode = _fixture(monkeypatch)
    mutations = {
        "non_owner": "UPDATE tasks SET created_by='default' WHERE id=?",
        "ended_run": "UPDATE tasks SET status='done' WHERE id=?",
        "dead_run": "UPDATE task_runs SET status='done' WHERE task_id=?",
    }
    if case in mutations:
        with kbc.connect_closing() as own:
            own.execute(mutations[case], (supervisor,))
            own.commit()
    environment = {
        "stale_run": ("HERMES_KANBAN_RUN_ID", "999999"),
        "child": ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
    }
    if case in environment:
        monkeypatch.setenv(*environment[case])
    if case == "direct_resume":
        from tools import kanban_containment_tools as tools
        out = json.loads(kt._kanban_handler("kanban_resume_contained")(tools.handle_resume)(
            {"task_id": relay, "board": slug, "episode": episode}))
    elif case == "shell":
        from hermes_cli.kanban import kanban_command
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        assert kanban_command(Namespace(kanban_action="reopen", task_id=authority, board=slug, reason="recovery")) != 0
        out = {"ok": False}
    else:
        out = _call("other" if case == "wrong_board" else slug,
                    other if case == "wrong_target" else relay,
                    other if case == "unrelated" else authority)
    assert out.get("ok") is not True, out
    with kbc.connect_closing(db_path=kb.board_own_db_path(slug)) as conn:
        assert kb.get_task(conn, authority).status == "done"
        assert kb.get_task(conn, relay).containment_episode == episode
