"""A park on ``hermes kanban wait-comment <card>`` re-probes when a comment lands on that card, not up to
``check_every`` later (t_01a1f5d00bc5fa08, 2026-11-20). [daveey]

A worker asked its orchestrator chat a design question and parked on ``wait-comment --after-id <its last comment>
--skip-system`` (check_every 900). The chat's answer arrived 6 s (23:12:51) and 2.5 min (03:11:13) after the two
parks, yet each probe slept to its next 15-minute slot; owner-wake meanwhile reported the card "waiting on chat"
and the chat unblocked it by hand both times.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import threading
import time

import pytest

from hermes_cli import kanban_awaited_card_wake as acw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_local_status_wake as lsw
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import kanban_sync_support as support

EVERY = 900


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    acw._reset_for_tests()
    conn = kbc.connect(home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()
        kwc._shutdown_for_tests()
        acw._reset_for_tests()


@pytest.fixture
def probes(monkeypatch):
    """Run each ``hermes kanban wait-comment`` probe in-process with the real ``run_wait_comment``."""
    from hermes_cli import kanban_comment_origin as kco

    runs: dict[str, int] = {}
    lock = threading.Lock()
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--after-id", dest="after_id", type=int, default=0)
    for flag in ("--from-gateway", "--contains", "--from-card"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--skip-system", dest="skip_system", action="store_true")

    def run_probe(key, cmd, cwd, env):
        argv = shlex.split(cmd)
        assert argv[:3] == ["hermes", "kanban", "wait-comment"], cmd
        with lock:
            runs[env["HERMES_KANBAN_TASK"]] = runs.get(env["HERMES_KANBAN_TASK"], 0) + 1
            saved = {k: os.environ.get(k) for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB",
                                                    "HERMES_KANBAN_WAKE_PROBE")}
            os.environ.update({k: env[k] for k in saved})
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    rc = kco.run_wait_comment(parser.parse_args(argv[3:]))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return rc, out.getvalue()

    monkeypatch.setattr(kwc, "_run_probe", run_probe)
    return runs


def _status(live, tid):
    task = kb.get_task(live, tid)
    assert task is not None
    return task.status


def _next_at(live, tid):
    state = kwc.get_state(live, tid)
    assert state is not None
    return state["next_at"]


def _park(live, probes, *, wake_check=None):
    """The worker comments its question, then parks on a comment after it, and runs its first probe."""
    tid = kb.create_task(live, title="coach self-heal backlog", assignee="worker")
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(live, tid, claimer="worker") is not None
    # A creator-comment park now requires a wakeable creator, not an unseen wait.
    # Keep the real park/probe path: this suite tests answer-to-probe latency.
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    chat_id = "20261120_000000_a1b2c3"
    sessions = SessionDB(get_hermes_home() / "state.db")
    try:
        if sessions.get_session(chat_id) is None:
            sessions.create_session(chat_id, source="desktop")
    finally:
        sessions.close()
    kbn.add_notify_sub(live, task_id=tid, platform="tui", chat_id=chat_id,
                      notifier_profile="default", delivery_metadata={"reason": "origin"})
    asked = kb.add_comment(live, tid, "default", "Question for the orchestrator: triage or clock?")
    cmd = wake_check or f"hermes kanban wait-comment {tid} --after-id {asked} --skip-system"
    assert kb.schedule_task(live, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on the chat's answer",
                            wake_check=cmd, check_every=EVERY)
    t0 = int(time.time())
    kwc.run_due_wake_checks(live, now=t0, wait=True)
    assert _status(live, tid) == "scheduled"
    assert _next_at(live, tid) == t0 + EVERY
    sync.consume_dispatcher_wake()
    return tid, t0


def test_the_answer_comment_wakes_the_park_within_a_minute(live, probes):
    tid, t0 = _park(live, probes)
    kb.add_comment(live, tid, "default", "Decision: rotate daily; proceed.")  # 03:11:13, the chat's answer
    assert sync.consume_dispatcher_wake()  # an in-process dispatcher ticks now
    assert _next_at(live, tid) <= t0 + 60  # before the fix: t0 + 900
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, tid) == "ready"
    met = [e for e in kb.list_events(live, tid) if e.kind == "condition_met"]
    assert met and "Decision: rotate daily" in (met[-1].payload or {})["output"]


def test_a_comment_the_probe_rejects_costs_one_probe_then_the_cadence(live, probes):
    tid, t0 = _park(live, probes)
    kb.add_comment(live, tid, "kanban-dispatcher", "system note", system=True)  # --skip-system: not an answer
    assert _next_at(live, tid) <= t0 + 60
    kwc.run_due_wake_checks(live, now=t0 + 30, wait=True)
    assert _status(live, tid) == "scheduled" and probes[tid] == 2
    assert _next_at(live, tid) == t0 + 30 + EVERY


def test_a_comment_on_another_card_or_a_non_comment_park_moves_nothing(live, probes):
    flag = os.path.join(os.path.dirname(str(kbc._main_db_file(live))), "never-written")
    other_park, t0 = _park(live, probes, wake_check=f"test -f {flag}")
    tid, t1 = _park(live, probes)
    unrelated = kb.create_task(live, title="unrelated", assignee="worker")
    kb.add_comment(live, unrelated, "default", "noise")
    kb.add_comment(live, other_park, "default", "a comment on a card whose park is not a comment wait")
    assert _next_at(live, tid) == t1 + EVERY and _next_at(live, other_park) == t0 + EVERY
    assert not sync.consume_dispatcher_wake()


def test_a_rolled_back_comment_expedites_nothing(live, probes):
    tid, t0 = _park(live, probes)
    with pytest.raises(RuntimeError), kb.write_txn(live):
        live.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'x', 'y', 0)", (tid,))
        raise RuntimeError("boom")
    assert _next_at(live, tid) == t0 + EVERY
    assert not sync.consume_dispatcher_wake()


def test_one_commit_with_a_comment_and_a_status_change_expedites_both(live, probes):
    """The comment expedite's own write txn re-arms the recorders: the status changes are read first."""
    tid, _t0 = _park(live, probes)
    review = kb.create_task(live, title="review", assignee="worker")
    owner = kb.create_task(live, title="owner", assignee="worker")
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'running' WHERE id IN (?, ?)", (review, owner))
    flag = os.path.join(os.path.dirname(str(kbc._main_db_file(live))), "never-written")
    assert kb.schedule_task(live, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting",
                            wake_check=f"test -f {flag}", check_every=EVERY,
                            awaiting={"kind": "card", "ref": review, "why": "review"})
    later = int(time.time()) + EVERY
    with kb.write_txn(live):
        live.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id IN (?, ?)", (later, tid, owner))
    with kb.write_txn(live):
        live.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'default', 'go', ?)",
                     (tid, int(time.time())))
        live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (review,))
    now = int(time.time())
    assert _next_at(live, tid) <= now and _next_at(live, owner) <= now


def test_a_failing_comment_expedite_never_fails_the_comment(live, probes, monkeypatch, caplog):
    tid, t0 = _park(live, probes)
    monkeypatch.setattr(lsw, "_COMMENT_DUE", "UPDATE no_such_table SET x = 1")
    cid = kb.add_comment(live, tid, "default", "answer")
    assert cid and _next_at(live, tid) == t0 + EVERY
    assert "could not expedite" in caplog.text
