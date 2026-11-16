"""Portable continuation references are scoped to the task and current run."""
import json
import subprocess

import pytest

from hermes_cli import kanban_continue_archive as archive
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli import test_kanban_pool_queue as helpers

svc = helpers.svc
w2 = helpers.w2


def _task(w2, monkeypatch):
    tid = kb.create_task(w2.conn, title="portable archive", assignee="default", runs_on=[helpers.POOL])
    helpers._run(w2, monkeypatch, tid)
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE tasks SET workspace_path = '/work' WHERE id = ?", (tid,))
    monkeypatch.setattr(kmp, "card_trees_state", lambda *a, **kw: (["/work"], "dirty worktree /work"))
    return tid, f"portable-workspace: hub-admin:/srv/{tid}/full.tar.gz SHA256 {'a' * 64}"


def _backdate_run(conn, tid, seconds):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = started_at - ? WHERE id = "
                     "(SELECT current_run_id FROM tasks WHERE id = ?)", (seconds, tid))


@pytest.mark.parametrize("source", ["checkpoint", "comment"])
@pytest.mark.parametrize("verified", [True, False])
def test_continue_archive(w2, monkeypatch, source, verified):
    from tools import kanban_tools as kt
    tid, text = _task(w2, monkeypatch)
    _backdate_run(w2.conn, tid, 5)
    monkeypatch.setattr(archive, "verify", lambda *args: verified)
    if source == "comment":
        kb.add_comment(w2.conn, tid, author="default", body=text)
        text = "Resume implementation"
    out = json.loads(kt._handle_continue({"checkpoint": text}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ([helpers.POOL] if verified else ["machine:worker-2"])
    assert bool(helpers._placement_events(w2.conn, tid)) is not verified


def test_no_archive_still_pins(w2, monkeypatch):
    from tools import kanban_tools as kt
    tid, _ = _task(w2, monkeypatch)
    out = json.loads(kt._handle_continue({"checkpoint": "Work remains local"}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ["machine:worker-2"]


@pytest.mark.parametrize("mutation", ["valid", "foreign", "traversal", "short-sha", "non-hub", "no-marker"])
def test_reference_scope(w2, monkeypatch, mutation):
    tid, text = _task(w2, monkeypatch)
    text = {
        "valid": text, "foreign": text.replace(tid, "t_other"),
        "traversal": text.replace("full.tar", "../full.tar"), "short-sha": text[:-1],
        "non-hub": text.replace("hub-admin:", "worker-2:"),
        "no-marker": text.replace("portable-workspace:", "diagnostics"),
    }[mutation]
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    assert archive.portable(w2.conn, tid, text) is (mutation == "valid")


def test_comments_after_move_and_no_old_fallback(w2, monkeypatch):
    tid, text = _task(w2, monkeypatch)
    _backdate_run(w2.conn, tid, 5)
    kb.add_comment(w2.conn, tid, author="default", body=text)
    with kb.write_txn(w2.conn):
        kb._append_event(w2.conn, tid, "moved", {})
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    assert not archive.portable(w2.conn, tid, "resume")
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE task_comments SET created_at = created_at + 1 WHERE task_id = ?", (tid,))
    assert archive.portable(w2.conn, tid, "resume")
    monkeypatch.setattr(archive, "verify", lambda path, sha, *args: sha == 'a' * 64)
    assert not archive.portable(w2.conn, tid, text.replace('a' * 64, 'b' * 64))


@pytest.mark.parametrize("mode", ["match", "mismatch", "missing", "timeout", "error", "extra-output", "local-change"])
def test_hub_inventory_verification(monkeypatch, tmp_path, mode):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 20 and kwargs["encoding"] == "utf-8"
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 20)
        if mode == "error":
            raise OSError("no hub")
        sha = 'a' * 64
        if (argv[0] == "hub-admin" and mode == "mismatch") or (len(calls) == 3 and mode == "local-change"):
            sha = 'b' * 64
        return subprocess.CompletedProcess(argv, 1 if mode == "missing" else 0,
                                           sha + ("noise" if mode == "extra-output" else "\n"))
    monkeypatch.setattr(archive.subprocess, "run", run)
    assert archive.verify("/srv/t_example/full.tar.gz", 'c' * 64, str(tmp_path)) is (mode == "match")
    if mode == "match":
        assert calls[1][0] == "hub-admin" and "archive" in calls[1][1]
        assert len(calls) == 3


def test_earlier_run_archive_comment_does_not_carry_to_next_run(w2, monkeypatch):
    from tools import kanban_tools as kt
    tid, text = _task(w2, monkeypatch)
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    _backdate_run(w2.conn, tid, 5)
    kb.add_comment(w2.conn, tid, author="default", body=text)
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE task_comments SET created_at = created_at - 2 WHERE task_id = ?", (tid,))
    assert json.loads(kt._handle_continue({"checkpoint": "run 1 resume"})).get("ok")
    assert helpers._runs_on(w2.conn, tid) == [helpers.POOL]
    helpers._run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_continue({"checkpoint": "run 2 resume"}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ["machine:worker-2"]
    assert helpers._placement_events(w2.conn, tid)


def test_comment_window_starts_at_current_run(w2, monkeypatch):
    tid, text = _task(w2, monkeypatch)
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    kb.add_comment(w2.conn, tid, author="default", body=text)
    assert not archive.portable(w2.conn, tid, "resume")
    _backdate_run(w2.conn, tid, 5)
    assert archive.portable(w2.conn, tid, "resume")
