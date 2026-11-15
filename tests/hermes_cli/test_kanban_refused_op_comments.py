"""A refused kanban op leaves no reason comment, says why, and free text is never parsed as ids. [daveey]

Regression for t_01a18653b77a4f83: two refused ``hermes kanban schedule`` calls each left a
``SCHEDULED: ...`` comment on a card that stayed ``blocked``; the refusal printed only
``cannot schedule <id>``; and ``unblock <id> "free text"`` unblocked the card before failing on
the free text as a second "id".
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


def _cli(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


_ASK = json.dumps({"action": "Unlock the iPhone", "why_human": "needs Face ID", "interaction": "physical",
                   "scope": "one unlock", "evidence": "device locked"})


def _human_blocked(conn, title="needs david") -> str:
    tid = kb.create_task(conn, title=title, assignee="a")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="David: unlock the iPhone", kind="needs_input")
    return tid


def _comments(conn, tid) -> list[str]:
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,))]


def _events(conn, tid, kind) -> int:
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind)).fetchone()[0]


def test_refused_schedule_leaves_no_comment_and_says_why(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _human_blocked(conn)
    rc = _cli("schedule", tid, "Parked on the wall", "--clock-only")
    err = capsys.readouterr().err
    assert rc == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert _comments(conn, tid) == []
        assert _events(conn, tid, "commented") == 0
    assert "blocked on a human answer (block_kind needs_input, blocked " in err
    assert "scheduling would hide that ask" in err
    assert f'hermes kanban unblock {tid} --reason "..." && hermes kanban schedule {tid}' in err


def test_refused_unblock_leaves_no_comment(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready card", assignee="a")
    rc = _cli("unblock", tid, "--reason", "not actually blocked")
    assert rc == 1
    assert "cannot unblock" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert _comments(conn, tid) == []
        assert _events(conn, tid, "unblocked") == 0


def test_successful_schedule_and_unblock_write_exactly_one_comment_each(kanban_home):
    with kbc.connect() as conn:
        tid = _human_blocked(conn)
    assert _cli("unblock", tid, "--reason", "ask moved to t_feedface") == 0
    with kbc.connect() as conn:
        assert _comments(conn, tid) == ["UNBLOCK: ask moved to t_feedface"]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        kb.claim_task(conn, tid)
    assert _cli("schedule", tid, "waiting", "on", "the", "cooldown", "--clock-only") == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _comments(conn, tid) == ["UNBLOCK: ask moved to t_feedface", "SCHEDULED: waiting on the cooldown"]
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('commented', 'scheduled', 'unblocked') "
            "ORDER BY id", (tid,))]
    # The comment still precedes its transition event (escalated_owner / block-loop checks read order).
    assert kinds == ["commented", "unblocked", "commented", "scheduled"]


def test_successful_block_writes_one_comment_before_its_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="to block", assignee="a")
        kb.claim_task(conn, tid)
    assert _cli("block", tid, "need", "the", "key", "--kind", "needs_input", "--human-request", _ASK) == 0
    with kbc.connect() as conn:
        assert _comments(conn, tid) == ["BLOCKED: need the key"]
        last_two = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 2", (tid,))]
    assert last_two == ["blocked", "commented"]


def test_refused_block_leaves_no_comment(kanban_home):
    with kbc.connect() as conn:
        tid = _human_blocked(conn)
        before = _comments(conn, tid)
    assert _cli("block", tid, "again", "--kind", "needs_input", "--human-request", _ASK) == 1
    with kbc.connect() as conn:
        assert _comments(conn, tid) == before


def test_unblock_free_text_is_rejected_before_any_change(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _human_blocked(conn)
    text = "Moving the API-credit ask to a card David sees"
    rc = _cli("unblock", tid, text)
    err = capsys.readouterr().err
    assert rc == 2
    assert f"not a task id: {text[:40]!r}" in err
    assert '--reason "..."' in err
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert _events(conn, tid, "unblocked") == 0
        assert _comments(conn, tid) == []


def test_unknown_id_is_rejected_before_any_change(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _human_blocked(conn)
    assert _cli("unblock", tid, "t_00000000deadbeef") == 2
    assert "unknown task id" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_schedule_ids_free_text_is_rejected_before_any_change(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready card", assignee="a")
    assert _cli("schedule", tid, "--clock-only", "--ids", "wait for the soak") == 2
    assert "not a task id: 'wait for the soak'" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert _comments(conn, tid) == []


def test_db_comment_rolls_back_with_a_raising_op(kanban_home):
    """The comment and the change share one txn: an op that raises keeps neither."""
    from hermes_cli import kanban_awaiting as kaw
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        with pytest.raises(kaw.AwaitingError):
            kb.schedule_task(conn, tid, reason="x", awaiting={"kind": "human"}, comment=("me", "SCHEDULED: x"))
        assert _comments(conn, tid) == []
        assert kb.get_task(conn, tid).status == "ready"


# --- kanban_schedule tool: a separate entry path -----------------------------------------------

@pytest.fixture
def worker_env(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        assert kb.block_task(conn, tid, reason="David: approve the spend", kind="needs_input")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_tool_refused_schedule_leaves_no_comment_and_says_why(worker_env):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_schedule({"reason": "Parked on the wall", "clock_only": True}))
    assert "blocked on a human answer (block_kind needs_input" in out.get("error", ""), out
    assert out.get("code") == "run_already_ended", out  # the worker's own block ended its run
    with kbc.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "blocked"
        assert _comments(conn, worker_env) == []
        assert _events(conn, worker_env, "commented") == 0
