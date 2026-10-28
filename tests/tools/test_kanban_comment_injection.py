"""Live operator-note injection into a running kanban worker.

``tools.kanban_tools.inject_new_comments_from_env`` polls the worker's task
for comments added *after* the run started and folds them into the live turn
via the agent's OUT-OF-BAND steer channel — so a user can talk to a running
task without the block→comment→unblock dance or a restart.

Verifies: no-op off a worker, watermark seeding (history isn't re-injected),
new comments steer, and own-authored comments are skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import tools.kanban_tools as kt


class FakeAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # Reset module-level poll state so tests don't leak into each other.
    kt._comment_watermark.clear()
    kt._comment_poll_last_attempt = 0.0
    return home


def _unthrottle():
    """Bypass the inter-poll rate limit for deterministic tests."""
    kt._comment_poll_last_attempt = 0.0


def test_noop_without_worker_env(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = FakeAgent()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_seed_then_inject_new_comment(worker_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="live task")
        kb.add_comment(conn, tid, author="desktop", body="pre-existing note")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    # First poll seeds the watermark past the existing thread — no injection.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="actually use the v2 API")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1
    assert "v2 API" in agent.steers[0]

    # Watermark advanced — a re-poll with no new comments injects nothing.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert len(agent.steers) == 1


def test_skips_own_authored_comments(worker_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="echo guard")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="worker-bot", body="i did a thing")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_delegated_child_in_worker_process_neither_receives_nor_consumes_notes(worker_home, monkeypatch):
    """A delegate_task child inherits the worker's ``HERMES_KANBAN_TASK``; operator notes
    address the worker, so the child must not be steered by them and must not advance the
    shared watermark (which would make the worker miss them) (#112817)."""
    from agent.delegation_context import delegated_child_context

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="live task")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")

    worker = FakeAgent()
    _unthrottle()
    assert kt.inject_new_comments_from_env(worker) is False  # seed

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="operator note for the worker")
    finally:
        conn.close()

    child = FakeAgent()
    with delegated_child_context():
        _unthrottle()
        assert kt.inject_new_comments_from_env(child) is False
    assert child.steers == []

    _unthrottle()
    assert kt.inject_new_comments_from_env(worker) is True
    assert "operator note" in worker.steers[0]


def test_skips_own_authored_comments_without_env_profile(worker_home, monkeypatch):
    """Own comments are skipped even when ``HERMES_PROFILE`` is absent: the
    injection filter resolves the worker's identity the same way the persisted
    write side does (env → ``HERMES_HOME``-derived active profile), so a worker's
    own notes can never steer its live turn as fake operator messages."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="identity echo guard")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    agent = FakeAgent()

    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    identity = kt._persisted_identity()
    assert identity != "worker"  # resolved from HERMES_HOME, not the generic label

    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author=identity, body="note to self")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


# --- Same-profile operator notes vs this run's own notes (echo filter keyed on comment ids) ---
#
# Every actor on a board usually runs as the same profile (``default``): Desktop chats,
# the chats overseeing workers, and the workers themselves. Filtering by author therefore
# dropped every operator note from a same-profile chat. Own notes are now the comments
# this RUN wrote (tool or CLI), recorded by id; everything else is delivered.

import json
import os
import subprocess


def _running_worker(monkeypatch, title="live task", profile="default"):
    """A claimed (running) task with this process set up as its dispatcher worker."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title)
        task = kb.claim_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", profile)
    return tid, run_id


def _as_other_session(fn):
    """Run ``fn`` as a different session on the SAME profile (an operator chat): no worker env."""
    saved = {k: os.environ.pop(k) for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID") if k in os.environ}
    try:
        return fn()
    finally:
        os.environ.update(saved)


def test_same_profile_operator_comment_is_injected(worker_home, monkeypatch):
    tid, _run = _running_worker(monkeypatch)
    agent = FakeAgent()
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False  # seed

    def post():
        conn = kbc.connect()
        try:
            return kb.add_comment(conn, tid, author="default", body="STOP the CPU burners now")
        finally:
            conn.close()
    _as_other_session(post)

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert "STOP the CPU burners" in agent.steers[0]
    # The steer names the author as another session and carries a time.
    assert "default (another session) at " in agent.steers[0]


def test_own_kanban_comment_tool_note_is_not_injected(worker_home, monkeypatch):
    tid, _run = _running_worker(monkeypatch)
    agent = FakeAgent()
    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "progress: step 2 done"}))
    assert out["ok"]
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_own_cli_comment_from_separate_process_is_not_injected(worker_home, monkeypatch):
    tid, _run = _running_worker(monkeypatch)
    agent = FakeAgent()
    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed
    # ``hermes kanban comment`` run by the worker's terminal: a separate process with the worker env.
    slash = f"comment {tid} note from my own shell"
    code = (f"import sys; sys.path.insert(0, {str(_WORKTREE)!r}); from hermes_cli import kanban; "
            f"print(kanban.run_slash({slash!r}))")
    proc = subprocess.run([sys.executable, "-c", code], env=dict(os.environ), capture_output=True,
                          text=True, timeout=120, check=False)
    assert "Comment added" in proc.stdout, proc.stdout + proc.stderr
    conn = kbc.connect()
    try:
        assert any("own shell" in c.body for c in kb.list_comments(conn, tid))
    finally:
        conn.close()
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_mixed_batch_injects_only_the_operator_note(worker_home, monkeypatch):
    tid, _run = _running_worker(monkeypatch)
    agent = FakeAgent()
    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed
    kt._handle_comment({"task_id": tid, "body": "my own progress note"})

    def post():
        conn = kbc.connect()
        try:
            kb.add_comment(conn, tid, author="default", body="operator: pause and re-read the card")
        finally:
            conn.close()
    _as_other_session(post)
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert "re-read the card" in agent.steers[0]
    assert "my own progress note" not in agent.steers[0]


def test_comment_delivered_event_records_ids_and_run(worker_home, monkeypatch):
    tid, run_id = _running_worker(monkeypatch)
    agent = FakeAgent()
    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    def post():
        conn = kbc.connect()
        try:
            return kb.add_comment(conn, tid, author="default", body="operator note")
        finally:
            conn.close()
    cid = _as_other_session(post)
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    conn = kbc.connect()
    try:
        delivered = [e for e in kb.list_events(conn, tid) if e.kind == "comment_delivered"]
    finally:
        conn.close()
    assert len(delivered) == 1
    assert delivered[0].payload == {"comment_ids": [cid], "run_id": run_id}
    assert delivered[0].run_id == run_id


def test_kanban_comment_reports_live_run_for_running_and_ready(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    conn = kbc.connect()
    try:
        running = kb.create_task(conn, title="running card")
        assert kb.claim_task(conn, running) is not None
        ready = kb.create_task(conn, title="ready card")
    finally:
        conn.close()
    out_running = json.loads(kt._handle_comment({"task_id": running, "body": "hello worker"}))
    out_ready = json.loads(kt._handle_comment({"task_id": ready, "body": "hello later"}))
    assert out_running["live_run"] is True
    assert "next model call" in out_running["delivery"]
    assert out_ready["live_run"] is False
    assert "delivery" not in out_ready


def test_kanban_show_lists_undelivered_live_run_comments(worker_home, monkeypatch):
    tid, _run = _running_worker(monkeypatch)
    kt._handle_comment({"task_id": tid, "body": "own note, never pending"})

    def post():
        conn = kbc.connect()
        try:
            return kb.add_comment(conn, tid, author="default", body="pending operator note")
        finally:
            conn.close()
    cid = _as_other_session(post)
    shown = json.loads(kt._handle_show({"task_id": tid}))
    assert shown["undelivered_comments"] == [cid]

    agent = FakeAgent()
    kt._comment_watermark[tid] = cid - 1  # watermark just below the operator note
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    shown = json.loads(kt._handle_show({"task_id": tid}))
    assert shown["undelivered_comments"] == []
