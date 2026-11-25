"""Status writes on a card another machine runs under its live Board lease are refused up front. [daveey]

Before: ``hermes kanban complete <id>`` on the creator (no local claim; the lease is only mirrored as
``lease_holder``/``lease_expires``) wrote ``done`` locally, closed the run mirror, queued a completion notice and
printed ``Completed``; then the Board rejected the push (``live_claim``) and the card read back ``running``
(David-MBP, t_01a1ccfeab615947, 2026-11-13). The run mirror is not fenced, so its rejected handoff stuck.

Now every CLI verb and kanban_* tool that writes a lifecycle column refuses before writing, names the holder and
leaves the database byte-for-byte as it was. ``--force`` does not bypass it: the Board rejects the write anyway.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_remote_archive import _running_on_worker
from tests.hermes_cli.test_kanban_worker_node import HUB, WORKER, Gateway


# The two-gateway fixtures of test_kanban_remote_archive: hub creates the card, worker-1 runs it under its lease.
@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


@pytest.fixture
def kills(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: (lambda pid, sig: sent.append((pid, sig))))
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: True)
    return sent


def _cli(line: str) -> tuple[int, str]:
    """Run ``hermes kanban <line>`` in-process; return (exit code, stdout+stderr)."""
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser
    parser = build_parser(argparse.ArgumentParser(add_help=False).add_subparsers(dest="_top"))
    args = parser.parse_args(shlex.split(line))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = kcli.kanban_command(args)
    return int(rc or 0), out.getvalue() + err.getvalue()


def _snapshot(conn) -> dict:
    """Every row of every table: the refusal must leave all of them unchanged."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')] for t in tables}


def _run_row(conn, tid):
    return tuple(conn.execute("SELECT r.status, r.outcome, r.summary, r.metadata, r.ended_at FROM tasks t "
                              "JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?", (tid,)).fetchone())


def _assert_refused(conn, tid, before, run_before, rc, out, verb):
    assert rc != 0, out
    assert f"cannot {verb} {tid}: it is running under {WORKER}'s live lease. Nothing changed." in out, out
    assert f"Let {WORKER}'s run finish, or archive it to stop it." in out, out
    assert _snapshot(conn) == before  # tasks, task_runs, task_events, kanban_sync_outbox, notify state: untouched
    assert kb.get_task(conn, tid).status == "running"
    assert _run_row(conn, tid) == run_before
    assert "completed" not in [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))]


def test_cli_complete_under_a_remote_lease_is_refused_and_writes_nothing(gws, svc, kills):
    """The incident: before the fix this exited 0, printed ``Completed`` and wrote ``done``."""
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    kbn.add_notify_sub(hub.conn, task_id=tid, platform="desktop", chat_id="chat-1")  # a subscriber to notify
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    rc, out = _cli(f"complete {tid} --summary x")
    _assert_refused(hub.conn, tid, before, run_before, rc, out, "complete")
    assert "Completed" not in out
    # The subscriber has no completion notice to deliver.
    _cursor, unseen = kbn.unseen_events_for_sub(hub.conn, task_id=tid, platform="desktop", chat_id="chat-1",
                                                kinds=["completed"])
    assert unseen == []
    hub.round()  # nothing reaches the Board to be rejected
    assert not hub.conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'sync_rejected'",
                                (tid,)).fetchone()


# Every CLI verb that writes a lifecycle column (status, current_run_uid, result, started_at, completed_at).
_CLI_LINES = [
    ("complete", "complete {tid} --summary x --force"),
    ("block", "block {tid} stop now --kind needs_input"),
    ("schedule", "schedule {tid} later"),
    ("unblock", "unblock {tid}"),
    ("request review", "request-review {tid} --summary x --force"),
    ("request changes", "request-changes {tid} redo it"),
    ("reopen review", "reopen-review {tid}"),
    ("reclaim", "reclaim {tid}"),
    ("move", "move {tid} --pool --reason x --reclaim"),
    ("reassign", "reassign {tid} other --reclaim"),
    ("assign", "assign {tid} other"),  # not a status write, but it changes who owns the holder's live run
    ("promote", "promote {tid}"),
    ("reopen", "reopen {tid} --reason x"),
    ("edit", "edit {tid} --result x"),
]


@pytest.mark.parametrize("verb,line", _CLI_LINES, ids=[v for v, _ in _CLI_LINES])
def test_every_cli_status_verb_under_a_remote_lease_is_refused(gws, svc, kills, verb, line):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    rc, out = _cli(line.format(tid=tid))
    _assert_refused(hub.conn, tid, before, run_before, rc, out, verb)


def test_cli_edit_of_title_under_a_remote_lease_still_works(gws, svc, kills):
    """Only status-moving edits are fenced; a title edit is not a lifecycle write."""
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    rc, out = _cli(f"edit {tid} --title renamed")
    assert rc == 0, out
    assert kb.get_task(hub.conn, tid).title == "renamed"


def test_a_bulk_cli_write_with_one_remote_leased_card_writes_none(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    other = kb.create_task(hub.conn, title="queued here", assignee="default", runs_on=["machine:hub"])
    before = _snapshot(hub.conn)
    rc, out = _cli(f"complete {other} {tid}")
    assert rc != 0 and WORKER in out, out
    assert _snapshot(hub.conn) == before


# Every kanban_* tool that writes a lifecycle column, called outside the card's own run.
_TOOL_CALLS = [
    ("complete", "_handle_complete", {"summary": "done elsewhere"}),
    ("block", "_handle_block", {"reason": "stop"}),
    ("unblock", "_handle_unblock", {}),
    ("schedule", "_handle_schedule", {"reason": "later"}),
    ("continue", "_handle_continue", {"checkpoint": "half"}),
    ("request review", "_handle_request_review", {"summary": "x"}),
    ("request changes", "_handle_request_changes", {"reason": "redo"}),
    ("move", "_handle_move", {"runs_on": ["pool:worker"], "reason": "x", "checkpoint": "y"}),
]


@pytest.mark.parametrize("verb,handler,extra", _TOOL_CALLS, ids=[h for _, h, _ in _TOOL_CALLS])
def test_every_lifecycle_tool_under_a_remote_lease_is_refused(gws, svc, kills, verb, handler, extra):
    from tools import kanban_tools
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    out = json.loads(getattr(kanban_tools, handler)({"task_id": tid, **extra}))
    assert out.get("rejection") == "live_claim" and out.get("holder") == WORKER, out
    assert out.get("code") == "remote_live_lease", out
    _assert_refused(hub.conn, tid, before, run_before, 1, out["error"], verb)


# --- regressions: no live remote lease, no refusal -------------------------------------------------------------

@pytest.mark.parametrize("line,verb", [("reclaim {tid}", "reclaim"), ("complete {tid} --summary x", "complete"), ("assign {tid} other", "assign")])
def test_cli_lapsed_remote_lease_still_fences_until_board_clears_it(gws, svc, kills, line, verb):
    # Board retains a dark holder's expired lease for up to 12h; timestamp expiry is not release.
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int(time.time() * 1000) - 1000, tid))
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    rc, out = _cli(line.format(tid=tid))
    _assert_refused(hub.conn, tid, before, run_before, rc, out, verb)
    assert "Reclaimed" not in out and "Completed" not in out
    assert kills == []


def test_cli_complete_after_board_clears_remote_lease_completes_as_before(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = NULL, lease_expires = NULL WHERE id = ?", (tid,))
    rc, out = _cli(f"complete {tid} --summary x")
    assert rc == 0 and f"Completed {tid}" in out, out
    assert kb.get_task(hub.conn, tid).status == "done"


def test_cli_complete_when_this_gateway_holds_the_lease_completes_as_before(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    own = str(sync._state(hub.conn)["gateway_id"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = ? WHERE id = ?", (own, tid))
    rc, out = _cli(f"complete {tid} --summary x")
    assert rc == 0 and f"Completed {tid}" in out, out
    assert kb.get_task(hub.conn, tid).status == "done"


def test_cli_assign_after_board_clears_remote_lease_assigns_as_before(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = NULL, lease_expires = NULL WHERE id = ?", (tid,))
    rc, out = _cli(f"assign {tid} other")
    assert rc == 0 and f"Assigned {tid} to other" in out, out
    assert kb.get_task(hub.conn, tid).assignee == "other"


def test_cli_assign_when_this_gateway_holds_the_lease_assigns_as_before(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    own = str(sync._state(hub.conn)["gateway_id"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = ? WHERE id = ?", (own, tid))
    rc, out = _cli(f"assign {tid} other")
    assert rc == 0 and f"Assigned {tid} to other" in out, out
    assert kb.get_task(hub.conn, tid).assignee == "other"
