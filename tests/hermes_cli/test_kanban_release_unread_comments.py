"""Manual releases must acknowledge another session's intervening comment. [daveey]"""
import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_SESSION_ID", "reader")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="release fixture", triage=True)
    return tid


def comment(tid, *, session="other", age=180, body="Leave this card in triage\nRetracted pick"):
    with kbc.connect_closing() as conn:
        cid = kb.add_comment(conn, tid, "default", body, writer_kind="agent", writer_session=session)
        at = int(time.time()) - age
        conn.execute("UPDATE task_comments SET created_at=? WHERE id=?", (at, cid))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='commented' "
                     "AND json_extract(payload, '$.comment_id')=?", (at, tid, cid))
        conn.commit()
    return cid


def snapshot(tid):
    with kbc.connect_closing() as conn:
        return (kb.get_task(conn, tid).status,
                list(conn.execute("SELECT id FROM task_events WHERE task_id=? ORDER BY id", (tid,))),
                list(conn.execute("SELECT id FROM task_comments WHERE task_id=? ORDER BY id", (tid,))))


def release(tid, entry, capsys, seen=None):
    if entry == "tool":
        from tools import kanban_tools as kt
        args = {"task_id": tid}
        if seen is not None:
            args["seen_comment_id"] = seen
        result = json.loads(kt._handle_unblock(args))
        return result.get("ok", False), json.dumps(result)
    args = argparse.Namespace(task_id=tid, task_ids=[tid], ids=None, reason=None,
                              dry_run=False, json=False, seen_comment_id=seen)
    rc = getattr(cli, "_cmd_" + entry)(args)
    assert rc in (0, 4), f"Unexpected release exit {rc}"
    output = capsys.readouterr()
    return rc == 0, output.out + output.err


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
def test_foreign_recent_refused_then_acknowledged(board, entry, capsys):
    cid = comment(board)
    before = snapshot(board)
    ok, output = release(board, entry, capsys)
    assert not ok
    assert "unread_comments" in output
    assert str(cid) in output and "Leave this card in triage" in output
    assert "other" in output and f"--seen {cid}" in output
    assert snapshot(board) == before
    ok, output = release(board, entry, capsys, cid)
    assert ok, output
    assert snapshot(board)[0] == "ready"


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
def test_seen_older_lists_only_newer(board, entry, capsys):
    older = comment(board, body="Old already-read decision")
    newer = comment(board, age=7200, body="Newer unread decision")
    before = snapshot(board)
    ok, output = release(board, entry, capsys, older)
    assert not ok
    assert "Newer unread decision" in output and str(newer) in output
    assert "Old already-read decision" not in output
    assert snapshot(board) == before


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("session,age", [("reader", 180), ("other", 3660), (None, 180)])
def test_allowed_comments(board, entry, session, age, capsys):
    comment(board, session=session, age=age)
    ok, output = release(board, entry, capsys)
    assert ok, output
    assert snapshot(board)[0] == "ready"


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
def test_no_caller_session_treats_recorded_writer_as_foreign(board, entry, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_SESSION_ID")
    comment(board, session="reader")
    ok, output = release(board, entry, capsys)
    assert not ok and "unread_comments" in output


@pytest.mark.parametrize("entry", ["promote", "unblock"])
def test_parser_seen_and_refusal_rollback_reason(board, entry, capsys):
    cid = comment(board)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    argv = ["kanban", entry, board]
    argv += ["operator reason"] if entry == "promote" else ["--reason", "operator reason"]
    args = parser.parse_args(argv)
    before = snapshot(board)
    assert getattr(cli, "_cmd_" + entry)(args) == 4
    assert snapshot(board) == before
    capsys.readouterr()
    args = parser.parse_args([*argv, "--seen", str(cid)])
    assert getattr(cli, "_cmd_" + entry)(args) == 0


def test_tool_uses_bound_session_not_process_identity(board, monkeypatch, capsys):
    from gateway.session_context import scoped_current_session_id
    monkeypatch.setenv("HERMES_SESSION_ID", "other")
    cid = comment(board, session="other")
    with scoped_current_session_id("reader"):
        ok, output = release(board, "tool", capsys)
        assert not ok and "unread_comments" in output
        ok, output = release(board, "tool", capsys, cid)
        assert ok, output


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
def test_listing_truncates_each_comment_at_300(board, entry, capsys):
    comment(board, body="x" * 300 + "MUST_NOT_APPEAR")
    comment(board, body="Second unread comment")
    ok, output = release(board, entry, capsys)
    assert not ok
    assert "x" * 300 in output and "MUST_NOT_APPEAR" not in output
    assert "Second unread comment" in output


@pytest.mark.parametrize("verb", ["promote", "unblock"])
def test_direct_system_release_is_unchanged(board, verb):
    comment(board)
    with kbc.connect_closing() as conn:
        if verb == "promote":
            assert kb.promote_task(conn, board, actor="system")[0]
        else:
            assert kb.unblock_task(conn, board)
        assert kb.get_task(conn, board).status == "ready"


@pytest.mark.parametrize("verb", ["promote", "unblock"])
def test_bulk_reports_successes_and_refusals_and_rejects_shared_seen(board, verb, capsys):
    cid = comment(board)
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", triage=True)
        last = kb.create_task(conn, title="last", triage=True)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    argv = ["kanban", verb, first]
    argv += ["--ids", board, last, "--json"] if verb == "promote" else [board, last]
    args = parser.parse_args([*argv, "--seen", str(cid)])
    assert getattr(cli, "_cmd_" + verb)(args) == 1
    assert all(snapshot(tid)[0] == "triage" for tid in (first, board, last))
    capsys.readouterr()
    args = parser.parse_args(argv)
    assert getattr(cli, "_cmd_" + verb)(args) == 4
    output = capsys.readouterr()
    assert all(tid in output.out + output.err for tid in (first, board, last))
    assert [snapshot(tid)[0] for tid in (first, board, last)] == ["ready", "triage", "ready"]
    if verb == "promote":
        result = json.loads(output.out)
        assert [r["promoted"] for r in result] == [True, False, True]
        assert result[1]["rejection"] == "unread_comments"


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
def test_guard_runs_under_write_lock(board, entry, monkeypatch, capsys):
    from hermes_cli import kanban_release_comments as guard
    original = guard.check
    calls = []

    def checked(conn, *args, **kwargs):
        assert conn.in_transaction
        calls.append(True)
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(guard, "check", checked)
    comment(board)
    ok, output = release(board, entry, capsys)
    assert not ok and "unread_comments" in output
    assert calls == [True]
