"""Kanban write results while this gateway's Board sync has given up its role. [daveey]

Incident (2026-11-13, David-MBP): the gateway gave up the sync role (``SchemaAhead``) at 23:53 PT. ``kanban_comment``
on t_01a1d3e55460fe21, whose live run was on worker-7, still returned "the card has a live worker run: it gets this
note at its next model call". The comment sat in the local outbox (pending, 0 attempts) and never reached the Board;
a chat told David a relayed request had been delivered. Same for 10761 and 10772.

The stopped state is produced the real way: ``run_forever`` passes its start check, then a round meets a board a
newer core migrated (this process declares an older core's columns) and records "gave up the sync role".
"""
from __future__ import annotations

import argparse
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_runstate as runstate
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tools import kanban_tools as kt

ME, BOARD, RUNNER = "macbook", "default", "worker-7"
LIVE_RUN_PROMISE = "gets this note at its next model call"


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(var, raising=False)
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    previous = sync.set_lease_client(support.FakeLeaseClient())
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    try:
        yield home, conn
    finally:
        conn.close()
        sync.set_lease_client(previous)


def _remote_running(conn) -> str:
    """A card running on worker-7 under its Board lease, as this gateway's mirror holds it."""
    tid = kb.create_task(conn, title="core update", assignee="default")
    assert kb.claim_task(conn, tid) is not None
    far = int((time.time() + 3600) * 1000)
    sync.apply_remote_changes(conn, [sync.RemoteChange(
        1, "tasks", tid, "lease", {"lease_holder": RUNNER, "lease_expires": far}, 9, "", RUNNER)], new_cursor=1)
    conn.execute("UPDATE tasks SET claim_lock = NULL, worker_pid = NULL WHERE id = ?", (tid,))
    assert kb.live_run_id(conn, tid) is not None
    return tid


def _give_up_the_sync_role(home, monkeypatch) -> None:
    """``run_forever``: the start check passes, then its first round meets a board a newer core migrated."""
    from hermes_cli import machine_metrics
    svc = FakeService()
    board_hub = svc.hub(ME)
    monkeypatch.setitem(sync.SYNCED_COLUMNS, "tasks", sync.SYNCED_COLUMNS["tasks"] - {"placement_policy"})
    monkeypatch.setattr(ksc, "_schema_ahead_boards", lambda boards: [])  # migrated after this loop started
    monkeypatch.setattr(ksc, "load_identity", lambda: board_hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: board_hub)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, home / "kanban.db")])
    monkeypatch.setattr(ksc, "reconcile_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: type(
        "R", (), {"report_due": lambda self: None, "pending_wake_in": lambda self: None})())
    monkeypatch.setattr(board_hub, "report_metrics", lambda *args: None, raising=False)
    monkeypatch.setattr(ksc, "FeedWatcher", type("Feed", (), {"__init__": lambda self, *a, **k: None,
                                                             "run": lambda self: None}))
    monkeypatch.setattr(ksc, "wait_for_work", lambda *args: None)
    ticks = iter(range(3))
    ksc.run_forever(lambda: next(ticks, None) is not None, interval=1)
    record = runstate.down()
    assert record and record["reason"].startswith("gave up the sync role") and "SchemaAhead" in record["reason"]


def _pending_comment_ops(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending' "
                        "AND table_name = 'task_comments'").fetchone()[0]


def test_comment_on_a_remote_run_while_the_sync_role_is_given_up_says_queued_locally(home, monkeypatch):
    path, conn = home
    tid = _remote_running(conn)
    _give_up_the_sync_role(path, monkeypatch)
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "please relay this request"}))
    assert out["ok"] is True and out["board_delivery"] == "queued_local"
    assert LIVE_RUN_PROMISE not in json.dumps(out)
    assert out["delivery"].startswith("queued locally: this gateway's Board sync has been stopped since ")
    assert "gave up the sync role" in out["delivery"] and "won't see this until it recovers" in out["delivery"]
    assert out["live_run_host"] == RUNNER and f"hermes kanban comment {tid}" in out["route"]
    assert RUNNER in out["delivery"]
    assert _pending_comment_ops(conn) >= 1  # the comment is written, just not pushed


def test_comment_while_the_sync_runs_is_unchanged(home):
    _path, conn = home
    tid = _remote_running(conn)
    assert runstate.down() is None
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "hello worker"}))
    assert set(out) == {"ok", "task_id", "comment_id", "live_run", "delivery"}
    assert out["live_run"] is True and LIVE_RUN_PROMISE in out["delivery"]


def test_comment_on_a_local_run_while_stopped_keeps_the_live_run_promise(home, monkeypatch):
    """The run reads this file, so it does get the note; only other hosts don't."""
    path, conn = home
    tid = kb.create_task(conn, title="runs here", assignee="default")
    assert kb.claim_task(conn, tid) is not None
    _give_up_the_sync_role(path, monkeypatch)
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "hello local worker"}))
    assert LIVE_RUN_PROMISE in out["delivery"] and out["board_delivery"] == "queued_local"
    assert "live_run_host" not in out and "route" not in out


def test_unblock_while_the_sync_role_is_given_up_says_queued_locally(home, monkeypatch):
    path, conn = home
    tid = kb.create_task(conn, title="waits on David", assignee="default")
    assert kb.block_task(conn, tid, reason="needs a decision")
    _give_up_the_sync_role(path, monkeypatch)
    out = json.loads(kt._handle_unblock({"task_id": tid}))
    assert "ok" in out, out
    assert out["ok"] is True and out["board_delivery"] == "queued_local"
    assert "gave up the sync role" in out["board_sync"]


def test_link_while_the_sync_role_is_given_up_says_queued_locally(home, monkeypatch):
    path, conn = home
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="child", assignee="default")
    _give_up_the_sync_role(path, monkeypatch)
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out["ok"] is True and out["board_delivery"] == "queued_local"
    assert out["board_sync"].startswith("queued locally: this gateway's Board sync has been stopped since ")


def test_link_while_the_sync_runs_is_unchanged(home):
    _path, conn = home
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="child", assignee="default")
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert "board_delivery" not in out and "board_sync" not in out


def test_unsynced_board_never_reports_queued_locally(tmp_path, monkeypatch):
    """Sync on for the host, but this board is not selected: the local file is the whole story."""
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(var, raising=False)
    home = support.kanban_home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    boards: [other]\n")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    runstate.record_down("gave up the sync role: board other: SchemaAhead(test)")
    assert runstate.down()
    conn = kbc.connect(home / "kanban.db")
    try:
        assert sync.board_mode(conn) == "off"
        tid = kb.create_task(conn, title="local only", assignee="default")
    finally:
        conn.close()
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "note"}))
    assert "board_delivery" not in out


def test_cli_comment_on_a_remote_run_while_stopped_drops_the_live_run_line(home, monkeypatch, capsys):
    from hermes_cli import kanban as kcli
    path, conn = home
    tid = _remote_running(conn)
    _give_up_the_sync_role(path, monkeypatch)
    import argparse
    rc = kcli._cmd_comment(argparse.Namespace(task_id=tid, text=["relay", "this"], max_len=None, author="me",
                                              origin_gateway=None, _slash_typed=True))
    printed = capsys.readouterr().out
    assert rc == 0 and LIVE_RUN_PROMISE not in printed
    assert "Queued locally: this gateway's Board sync has been stopped since " in printed
    assert f"on {RUNNER}" in printed


def test_cli_link_while_stopped_prints_the_notice(home, monkeypatch, capsys):
    from hermes_cli import kanban as kcli
    path, conn = home
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="child", assignee="default")
    _give_up_the_sync_role(path, monkeypatch)
    import argparse
    assert kcli._cmd_link(argparse.Namespace(parent_id=parent, child_id=child)) == 0
    assert "Queued locally:" in capsys.readouterr().out


def test_cli_unblock_while_stopped_prints_the_notice_once(home, monkeypatch, capsys):
    from hermes_cli import kanban as kcli
    path, conn = home
    tids = [kb.create_task(conn, title=f"waits {i}", assignee="default") for i in range(2)]
    for tid in tids:
        assert kb.block_task(conn, tid, reason="needs a decision")
    _give_up_the_sync_role(path, monkeypatch)
    import argparse
    assert kcli._cmd_unblock(argparse.Namespace(task_ids=tids, reason=None)) == 0
    assert capsys.readouterr().out.count("Queued locally:") == 1
    assert kcli._cmd_unblock(argparse.Namespace(task_ids=tids, reason=None)) == 1  # nothing written this time
    assert "Queued locally:" not in capsys.readouterr().out


def _in_review(conn) -> str:
    tid = kb.create_task(conn, title="needs review", assignee="default")
    assert kb.claim_task(conn, tid) is not None
    run = kb.latest_run(conn, tid)
    assert run is not None and kb.request_review(conn, tid, expected_run_id=run.id)
    return tid


def _cli_archive(conn):
    tid = kb.create_task(conn, title="drop me", assignee="default")
    return "_cmd_archive", argparse.Namespace(task_ids=[tid], purge_ids=None), f"Archived {tid}"


def _cli_reopen(conn):
    tid = kb.create_task(conn, title="done too soon", assignee="default")
    assert kb.complete_task(conn, tid, summary="first pass")
    return "_cmd_reopen", argparse.Namespace(task_id=tid, reason="missed a case"), f"Reopened {tid}"


def _cli_reopen_review(conn):
    tid = _in_review(conn)
    return "_cmd_reopen_review", argparse.Namespace(task_ids=[tid], reason=None), f"Reopened {tid}"


def _cli_request_review(conn):
    tid = kb.create_task(conn, title="implemented", assignee="default")
    assert kb.claim_task(conn, tid) is not None
    return ("_cmd_request_review", argparse.Namespace(task_id=tid, summary="tests pass", metadata=None,
                                                      reviewer=None, force=True), f"Requested review for {tid}")


def _cli_request_changes(conn):
    tid = _in_review(conn)
    assert kb.claim_review_task(conn, tid) is not None
    return ("_cmd_request_changes", argparse.Namespace(task_id=tid, reason=["fix", "the", "nit"]),
            f"Requested changes for {tid}")


def _cli_unlink(conn):
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="child", assignee="default")
    kb.link_tasks(conn, parent, child)
    return "_cmd_unlink", argparse.Namespace(parent_id=parent, child_id=child), f"Unlinked {parent} -> {child}"


CLI_WRITES = [_cli_archive, _cli_reopen, _cli_reopen_review, _cli_request_review, _cli_request_changes, _cli_unlink]


@pytest.mark.parametrize("setup", CLI_WRITES, ids=lambda f: f.__name__[5:])
def test_cli_write_while_stopped_prints_the_notice_once(home, monkeypatch, capsys, setup):
    """CLI parity with the tools (review 5888858852): every CLI write the stopped sync keeps local says so."""
    from hermes_cli import kanban as kcli
    path, conn = home
    command, args, done = setup(conn)
    _give_up_the_sync_role(path, monkeypatch)
    assert getattr(kcli, command)(args) == 0
    printed = capsys.readouterr().out
    assert done in printed, printed
    assert printed.count("Queued locally: this gateway's Board sync has been stopped since ") == 1, printed
    assert "gave up the sync role" in printed


@pytest.mark.parametrize("setup", CLI_WRITES, ids=lambda f: f.__name__[5:])
def test_cli_write_while_the_sync_runs_prints_no_notice(home, capsys, setup):
    from hermes_cli import kanban as kcli
    _path, conn = home
    command, args, done = setup(conn)
    assert getattr(kcli, command)(args) == 0
    printed = capsys.readouterr().out
    assert done in printed and "Queued locally" not in printed, printed


def test_cli_unlink_of_no_link_while_stopped_prints_no_notice(home, monkeypatch, capsys):
    """Nothing was written, so nothing is queued."""
    from hermes_cli import kanban as kcli
    path, conn = home
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="child", assignee="default")
    _give_up_the_sync_role(path, monkeypatch)
    assert kcli._cmd_unlink(argparse.Namespace(parent_id=parent, child_id=child)) == 1
    assert "Queued locally" not in capsys.readouterr().out


def test_reads_and_refusals_while_stopped_are_unchanged(home, monkeypatch):
    path, conn = home
    tid = _remote_running(conn)
    _give_up_the_sync_role(path, monkeypatch)
    assert "board_delivery" not in json.loads(kt._handle_attachments({"task_id": tid}))  # a read, also ok: true
    refused = json.loads(kt._handle_attach_url({"task_id": tid, "url": "ftp://example.invalid/x"}))
    assert "error" in refused and "board_delivery" not in refused


CHAT = "20261026_235722_bef6c6"
SHOWS_PROMISE = "shows in this chat's tasks."


def _place_on_worker(home, monkeypatch, *, task_id):
    """kanban_create from a chat, placed on worker-4 (01:09 PT: t_01a1d42acdce8e23 and three more). ``task_id``
    None = the target has not answered yet (the queued-request note)."""
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_placement as kp
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id=CHAT, source="desktop")
    db.close()
    monkeypatch.setenv("HERMES_SESSION_ID", CHAT)
    monkeypatch.setattr(reg, "self_gateway", lambda: ME)
    monkeypatch.setattr(runstate, "placement_runs_on", lambda conn, placement: None)  # worker-4: a gateway pin
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "worker-4", "why": "explicit placement"})
    monkeypatch.setattr(kp, "create_remote", lambda args, **kw: {
        "ok": True, "request_id": "placement:macbook:t1", "queued_only": task_id is None})
    monkeypatch.setattr(kp, "await_task_id", lambda rid, **kw: (task_id, "running"))
    return json.loads(kt._handle_create({"title": "core update", "assignee": "default", "placement": "worker-4",
                                         "body": "runs on worker-4", "requires": []}))


def test_create_placed_elsewhere_while_the_sync_role_is_given_up_says_the_chat_link_is_queued(home, monkeypatch):
    path, _conn = home
    _give_up_the_sync_role(path, monkeypatch)
    out = _place_on_worker(path, monkeypatch, task_id="t_01a1d42acdce8e23")
    assert out["ok"] is True and out["task_id"] == "t_01a1d42acdce8e23" and out["runs_where"] == "worker-4", out
    assert out["placed"]["origin_session"] == CHAT
    assert out["chat_link"] == "queued_local" and "board_delivery" not in out  # the card itself is on worker-4
    assert SHOWS_PROMISE not in out["note"]
    assert "shows in this chat's tasks once this gateway's Board sync recovers" in out["note"]
    assert "queued locally: this gateway's Board sync has been stopped since " in out["note"]
    assert "gave up the sync role" in out["board_sync"]


def test_create_queued_on_a_target_while_stopped_says_the_chat_link_waits(home, monkeypatch):
    path, _conn = home
    _give_up_the_sync_role(path, monkeypatch)
    out = _place_on_worker(path, monkeypatch, task_id=None)
    assert out["ok"] is True and out["status"] == "queued" and out["chat_link"] == "queued_local", out
    assert "(this chat's tasks once this gateway's Board sync recovers, " in out["note"]


@pytest.mark.parametrize("task_id", ["t_01a1d42acdce8e23", None], ids=["created", "queued"])
def test_create_note_wording_and_its_stopped_sync_rewrite_share_one_source(home, monkeypatch, task_id):
    """Review 5888858852: the rewrite anchored on literals kanban_tools also spelled out, so rewording the note
    there made it silently do nothing. Reworded at the one source, the rewrite still lands."""
    from tools import kanban_tools_board_delivery as kbd
    monkeypatch.setattr(kbd, "CHAT_TASKS_SHOWS", " and is listed under this chat")
    monkeypatch.setattr(kbd, "CHAT_TASKS_QUEUED", "(listed under this chat, ")
    path, _conn = home
    _give_up_the_sync_role(path, monkeypatch)
    out = _place_on_worker(path, monkeypatch, task_id=task_id)
    assert out["chat_link"] == "queued_local", out
    later = "once this gateway's Board sync recovers"
    expected = (f" and is listed under this chat {later}." if task_id else f"(listed under this chat {later}, ")
    assert expected in out["note"], out["note"]
    assert "this chat's tasks" not in out["note"]


def test_create_placed_elsewhere_while_the_sync_runs_is_unchanged(home, monkeypatch):
    path, _conn = home
    out = _place_on_worker(path, monkeypatch, task_id="t_01a1d42acdce8e23")
    assert out["ok"] is True and SHOWS_PROMISE in out["note"], out
    assert "chat_link" not in out and "board_sync" not in out and "board_delivery" not in out


def test_a_finished_remote_card_has_no_live_run_host(home):
    from hermes_cli.kanban_board_delivery import remote_runner
    _path, conn = home
    tid = _remote_running(conn)
    assert remote_runner(conn, tid) == RUNNER
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert remote_runner(conn, tid) is None
