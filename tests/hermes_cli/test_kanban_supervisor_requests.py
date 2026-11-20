"""Durable supervisor requests: scoped worker -> non-worker executor graph edits. [daveey]

Every test runs on an isolated board under ``tmp_path`` (``HERMES_HOME`` + the
``HERMES_KANBAN_*`` pins the root conftest blanks), never the live board.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_supervisor_requests as ksr

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A worker run for ``requester`` plus a graph of cards its task created."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    db = kb.kanban_db_path()
    assert str(db).startswith(str(tmp_path)), db  # never the live board
    conn = kbc.connect()
    try:
        requester = kb.create_task(conn, title="standing support", assignee="default", created_by="default")
        made = {name: kb.create_task(conn, title=name, assignee="default", created_by="default",
                                     creator_task_id=requester)
                for name in ("qa119", "dev116", "qa116", "dev67", "dev81")}
        kb.link_tasks(conn, made["qa119"], made["dev116"])
        kb.link_tasks(conn, made["dev67"], made["dev81"])
        foreign = kb.create_task(conn, title="someone else's", assignee="default")
        kb.claim_task(conn, requester)
        run_id = kb._current_run_id(conn, requester)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", requester)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"requester": requester, "run_id": run_id, "foreign": foreign, "db": str(db),
            "home": str(home), "tmp": tmp_path, **made}


def _tool(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(args))


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _reorder(b):
    return [{"op": "unlink", "parent_id": b["qa119"], "child_id": b["dev116"]},
            {"op": "unlink", "parent_id": b["dev67"], "child_id": b["dev81"]}]


def _as_worker(monkeypatch, b):
    monkeypatch.setenv("HERMES_KANBAN_TASK", b["requester"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(b["run_id"]))


def _edges(conn):
    return {(r[0], r[1]) for r in conn.execute("SELECT parent_id, child_id FROM task_links")}


def _events(conn, tid, prefix="supervisor_request_"):
    return [(e.kind, e.payload) for e in kb.list_events(conn, tid) if e.kind.startswith(prefix)]


def _park(kt, rid):
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    return json.loads(kt._handle_schedule({"reason": f"SCHEDULED_UNTIL={until} waiting on supervisor request #{rid}",
                                           "awaiting": {"kind": "external", "ref": f"supervisor request #{rid}",
                                                        "why": "the supervisor request executor runs it"}}))


def _child_env(b):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(HERMES_HOME=b["home"], HOME=str(b["tmp"]), HERMES_KANBAN_DB=b["db"], PYTHONPATH=str(REPO))
    env.pop("HERMES_KANBAN_TASK", None)
    return env


# --- the reproduced wall and the invariant that stays ---------------------------------------

def test_same_profile_creator_and_assignee_has_no_capable_supervisor(board):
    """Reproduction: creator == assignee == default, no kanban.orchestrator_profile."""
    from hermes_cli import kanban_assignment_routing as ar
    with kbc.connect() as conn:
        task = kb.get_task(conn, board["requester"])
        with pytest.raises(ar.AssignmentRoutingError, match="no_capable_supervisor"):
            with kb.write_txn(conn):
                ar.route(conn, task, {"reason": "need a sibling unlink"})


def test_worker_sibling_unlink_is_still_denied_and_names_the_route(board):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_unlink({"parent_id": board["qa119"], "child_id": board["dev116"]}))
    assert "only unlink edges of its own card" in out["error"]
    assert "kanban_supervisor_request" in out["error"]
    with kbc.connect() as conn:
        assert (board["qa119"], board["dev116"]) in _edges(conn)


def test_worker_cli_sibling_unlink_is_denied_too(board, capsys):
    """The board CLI from a worker's shell is not a way around the tool's refusal."""
    from types import SimpleNamespace
    from hermes_cli.kanban import _cmd_unlink
    assert _cmd_unlink(SimpleNamespace(parent_id=board["qa119"], child_id=board["dev116"])) != 0
    assert "kanban_supervisor_request" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert (board["qa119"], board["dev116"]) in _edges(conn)


# --- origin and scope ------------------------------------------------------------------------

def test_non_worker_origin_is_refused(board, monkeypatch):
    _as_executor(monkeypatch)
    out = _tool({"ops": _reorder(board)})
    assert out.get("ok") is not True and "not_a_worker" in json.dumps(out)


def test_stale_run_is_refused(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"] + 99))
    out = _tool({"ops": _reorder(board)})
    assert out["code"] == "stale_run", out
    with kbc.connect() as conn:
        assert ksr.list_requests(conn) == []


def test_delegated_child_is_refused(board, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_delegation_ctx", lambda name, default: True)
    out = _tool({"ops": _reorder(board)})
    assert "delegate_task child" in out["error"]


def test_foreign_archived_link_and_cycle_ops_are_refused(board):
    b = board
    with kbc.connect() as conn:
        kb.link_tasks(conn, b["foreign"], b["dev67"])
    out = _tool({"ops": [{"op": "unlink", "parent_id": b["foreign"], "child_id": b["dev67"]}]})
    assert out["code"] == "refused" and "not created by" in out["error"]
    with kbc.connect() as conn:
        kb.archive_task(conn, b["dev81"])
    out = _tool({"ops": [{"op": "unlink", "parent_id": b["dev67"], "child_id": b["dev81"]}]})
    assert out["code"] == "refused" and "archived" in out["error"]
    out = _tool({"ops": [{"op": "link", "parent_id": b["qa116"], "child_id": b["dev67"]}]})
    assert out["code"] == "link_not_requested" and "kanban_link" in out["error"]
    out = _tool({"ops": [{"op": "link", "parent_id": b["dev116"], "child_id": b["qa119"]}], "dry_run": True})
    assert out["ops"][0]["verdict"] == "refused" and "cycle" in out["ops"][0]["reason"]
    out = _tool({"ops": [{"op": "unlink", "parent_id": b["requester"], "child_id": b["qa116"]}]})
    assert out["code"] in ("own_card_op", "no_supervisor_needed"), out
    with kbc.connect() as conn:
        assert ksr.list_requests(conn) == []


def test_dry_run_preflight_separates_tool_names_from_effective_scope(board, monkeypatch):
    b = board
    with kbc.connect() as conn:
        kb.link_tasks(conn, b["requester"], b["qa116"])
        kb.link_tasks(conn, b["foreign"], b["dev67"])
    ops = [{"op": "unlink", "parent_id": b["qa119"], "child_id": b["dev116"]},
           {"op": "unlink", "parent_id": b["requester"], "child_id": b["qa116"]},
           {"op": "link", "parent_id": b["qa116"], "child_id": b["dev67"]},
           {"op": "unlink", "parent_id": b["qa116"], "child_id": b["dev67"]},
           {"op": "unlink", "parent_id": b["qa116"], "child_id": b["dev81"]},
           {"op": "unlink", "parent_id": b["foreign"], "child_id": b["dev67"]}]
    out = _tool({"ops": ops, "dry_run": True})
    assert out["ok"] and out["dry_run"]
    # unlink of a created->created edge needs the supervisor; own-card unlinks and links are
    # the worker's own tools; a link earlier in the batch makes a later unlink of it in scope
    assert [v["verdict"] for v in out["ops"]] == ["supervisor", "self", "self", "supervisor", "noop", "refused"]
    assert out["executor_available"] is False
    with kbc.connect() as conn:
        assert ksr.list_requests(conn) == []
        _as_executor(monkeypatch)
        ksr.drain(conn)
        assert ksr.executor_available(conn) is True


# --- queueing, delivery and receipts ---------------------------------------------------------

def test_absent_executor_leaves_a_visible_agent_owned_pending_request(board):
    from tools import kanban_tools as kt
    out = _tool({"ops": _reorder(board), "reason": "#116 next"})
    assert out["ok"] and out["status"] == "pending" and out["executor_available"] is False
    assert "not a human" in out["next_step"]
    parked = _park(kt, out["request_id"])
    assert parked.get("ok"), parked
    with kbc.connect() as conn:
        task = kb.get_task(conn, board["requester"])
        assert task.status == "scheduled" and task.block_kind != "needs_input" and task.human_request is None
        assert ksr.get_request(conn, out["request_id"])["status"] == "pending"
        assert _events(conn, board["requester"])[0][0] == "supervisor_request_queued"
    shown = json.loads(kt._handle_show({"task_id": board["requester"]}))
    assert [r["status"] for r in shown["supervisor_requests"]] == ["pending"]


def test_duplicate_requests_return_the_same_row(board, monkeypatch):
    first = _tool({"ops": _reorder(board)})
    again = _tool({"ops": _reorder(board)})
    assert again["request_id"] == first["request_id"] and again["duplicate"] is True
    with kbc.connect() as conn:
        assert len(ksr.list_requests(conn)) == 1
        _as_executor(monkeypatch)
        assert ksr.drain(conn)["processed"] == [(first["request_id"], "done")]
    _as_worker(monkeypatch, board)
    with kbc.connect() as conn:  # the edges came back since: the same ops are a new request
        kb.link_tasks(conn, board["qa119"], board["dev116"])
        kb.link_tasks(conn, board["dev67"], board["dev81"])
    again = _tool({"ops": _reorder(board)})
    assert again["request_id"] != first["request_id"] and again["duplicate"] is False


def test_executor_applies_and_correlates_receipts_and_wakes_without_releasing(board, monkeypatch):
    from tools import kanban_tools as kt
    b = board
    with kbc.connect() as conn:
        waiting_child = kb.create_task(conn, title="after support", parents=[b["requester"]])
    out = _tool({"ops": _reorder(b)})
    assert _park(kt, out["request_id"])["ok"]
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        res = ksr.drain(conn)
        assert res["processed"] == [(out["request_id"], "done")]
        assert _edges(conn) == {(b["requester"], waiting_child)}
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done" and req["executor"] == res["executor"] and req["attempts"] == 1
        assert [r["outcome"] for r in req["result"]] == ["applied"] * 2
        kinds = [k for k, _ in _events(conn, b["requester"])]
        assert kinds == ["supervisor_request_queued", "supervisor_request_accepted", "supervisor_request_done",
                         "supervisor_request_wake"]
        assert all(p["request_id"] == out["request_id"] for _, p in _events(conn, b["requester"]))
        comments = [c for c in kb.list_comments(conn, b["requester"]) if c.author == ksr.COMMENT_AUTHOR]
        assert len(comments) == 1 and f"#{out['request_id']} done" in comments[0].body
        requester = kb.get_task(conn, b["requester"])
        assert requester.status == "ready"  # woken, not completed
        assert kb.get_task(conn, waiting_child).status == "todo"  # its acceptance still gates children
        # the edits went through the ordinary link/unlink paths (their own board events)
        assert any(e.kind == "unlinked" for e in kb.list_events(conn, b["dev116"]))
        assert any(e.kind == "unlinked" for e in kb.list_events(conn, b["dev81"]))


def test_rejected_and_failed_requests_report_without_releasing(board, monkeypatch):
    from tools import kanban_tools as kt
    b = board
    with kbc.connect() as conn:
        waiting_child = kb.create_task(conn, title="after support", parents=[b["requester"]])
    rejected = _tool({"ops": [{"op": "unlink", "parent_id": b["dev67"], "child_id": b["dev81"]}]})
    failing = _tool({"ops": [{"op": "unlink", "parent_id": b["qa119"], "child_id": b["dev116"]}]})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        kb.archive_task(conn, b["dev81"])  # scope changed after submit: re-checked at execution
    monkeypatch.setattr(kt, "_handle_unlink", lambda args, **kw: json.dumps({"error": "injected unlink failure"}))
    with kbc.connect() as conn:
        res = ksr.drain(conn)
        assert res["processed"] == [(rejected["request_id"], "rejected"), (failing["request_id"], "failed")]
        assert "archived" in ksr.get_request(conn, rejected["request_id"])["error"]
        assert "injected" in ksr.get_request(conn, failing["request_id"])["error"]
        assert (b["qa119"], b["dev116"]) in _edges(conn)
        requester = kb.get_task(conn, b["requester"])
        assert requester.status == "running" and requester.completed_at is None
        assert kb.get_task(conn, waiting_child).status == "todo"
        kinds = [k for k, _ in _events(conn, b["requester"])]
        assert "supervisor_request_rejected" in kinds and "supervisor_request_failed" in kinds
    # a failed request may be filed again (not deduplicated against failures)
    _as_worker(monkeypatch, b)
    retry = _tool({"ops": [{"op": "unlink", "parent_id": b["qa119"], "child_id": b["dev116"]}]})
    assert retry["request_id"] != failing["request_id"] and retry["duplicate"] is False


def test_busy_executor_keeps_the_request_queued_then_lease_expiry_redelivers_once(board, monkeypatch):
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        busy = f"{ksr.socket.gethostname()}:{sleeper.pid}:dispatcher"
        now = int(time.time())
        with kbc.connect() as conn:
            with kb.write_txn(conn):
                conn.execute("UPDATE supervisor_requests SET status='accepted', executor=?, lease_expires=?, "
                             "attempts=1 WHERE id=?", (busy, now + 60, out["request_id"]))
            assert ksr.drain(conn, now=now)["processed"] == []
            assert ksr.get_request(conn, out["request_id"])["executor"] == busy
            assert (board["qa119"], board["dev116"]) in _edges(conn)
            later = now + ksr.LEASE_SECONDS + 61
            assert ksr.drain(conn, now=later)["processed"] == [(out["request_id"], "done")]
            assert ksr.drain(conn, now=later)["processed"] == []
            req = ksr.get_request(conn, out["request_id"])
            assert req["attempts"] == 2 and req["status"] == "done"
            accepted = [p for k, p in _events(conn, board["requester"]) if k == "supervisor_request_accepted"]
            assert accepted[-1]["redelivered_from"] == busy
            assert sum(1 for e in kb.list_events(conn, board["dev116"]) if e.kind == "unlinked") == 1
    finally:
        sleeper.kill()
        sleeper.wait()


def test_executor_refuses_inside_a_worker_and_the_tick_skips(board):
    _tool({"ops": _reorder(board)})
    with kbc.connect() as conn:
        with pytest.raises(ksr.SupervisorRequestError, match="worker_context"):
            ksr.drain(conn)
        assert ksr.dispatcher_drain(conn) == []
        assert [r["status"] for r in ksr.list_requests(conn)] == ["pending"]


def test_dispatcher_tick_is_the_executor(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        dry = kbd.dispatch_once(conn, dry_run=True, max_spawn=0)
        assert dry.supervisor_requests == []
        assert ksr.get_request(conn, out["request_id"])["status"] == "pending"
        res = kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
        assert res.supervisor_requests == [(out["request_id"], "done")]
        assert _edges(conn) == set()


def test_dispatcher_drain_honours_the_kill_switch(board, monkeypatch):
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    from hermes_cli import config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly",
                        lambda: {"kanban": {"supervisor_requests": {"enabled": False}}})
    with kbc.connect() as conn:
        assert ksr.dispatcher_drain(conn) == []
        assert ksr.get_request(conn, out["request_id"])["status"] == "pending"


# --- real processes: crash/restart and end to end --------------------------------------------

_CRASHING_EXECUTOR = """
import os, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_supervisor_requests as ksr
with kbc.connect() as conn:
    ksr.drain(conn, role="crashy", on_accept=lambda req: os._exit(17))
"""


def _cli_drain(b):
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "supervisor-requests", "--drain", "--json"],
        env=_child_env(b), cwd=str(REPO), capture_output=True, text=True, timeout=240, check=False)


def test_crash_after_accept_is_redelivered_by_the_next_executor(board):
    out = _tool({"ops": _reorder(board)})
    crashed = subprocess.run([sys.executable, "-c", _CRASHING_EXECUTOR, str(REPO)],
                             env=_child_env(board), capture_output=True, text=True, timeout=240, check=False)
    assert crashed.returncode == 17, crashed.stderr[-2000:]
    with kbc.connect() as conn:
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "accepted" and req["executor"].endswith(":crashy")
        assert (board["qa119"], board["dev116"]) in _edges(conn)  # nothing applied before the crash
    restarted = _cli_drain(board)
    assert restarted.returncode == 0, restarted.stderr[-2000:]
    report = json.loads(restarted.stdout[restarted.stdout.index("{"):])
    assert report["drained"]["processed"] == [[out["request_id"], "done"]]
    with kbc.connect() as conn:
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done" and req["attempts"] == 2 and req["executor"].endswith(":cli")
        assert (board["qa119"], board["dev116"]) not in _edges(conn)


def test_end_to_end_worker_request_to_separate_non_worker_executor_process(board):
    """The Eve-shaped reorder: a worker adds the new edges itself with kanban_link, files the
    unlinks it may not do through the registered tool, and a separate process with no worker
    env (the CLI executor) accepts them and changes the fixture graph with the ordinary tools."""
    from tools.registry import registry
    import tools.kanban_tools  # registers the tool
    b = board
    for p, c in ((b["qa116"], b["dev67"]), (b["qa119"], b["dev81"])):
        assert json.loads(registry.dispatch("kanban_link", {"parent_id": p, "child_id": c}))["ok"]
    denied = json.loads(registry.dispatch("kanban_unlink", {"parent_id": b["qa119"], "child_id": b["dev116"]}))
    assert "kanban_supervisor_request" in denied["error"]
    out = json.loads(registry.dispatch("kanban_supervisor_request", {"ops": _reorder(board)}))
    assert out["ok"] and out["status"] == "pending", out
    proc = _cli_drain(board)
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert report["drained"]["processed"] == [[out["request_id"], "done"]]
    assert str(os.getpid()) not in report["drained"]["executor"]  # a different process executed it
    with kbc.connect() as conn:
        assert _edges(conn) == {(b["qa116"], b["dev67"]), (b["qa119"], b["dev81"])}
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done" and req["executor"] == report["drained"]["executor"]
        kinds = [k for k, _ in _events(conn, board["requester"])]
        assert kinds[-2:] == ["supervisor_request_accepted", "supervisor_request_done"]
        assert kb.get_task(conn, board["requester"]).status == "running"


# --- review findings -------------------------------------------------------------------------

def test_result_before_park_still_wakes_the_requester_once(board, monkeypatch):
    from tools import kanban_tools as kt
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "done")]
        assert kb.get_task(conn, board["requester"]).status == "running"
    _as_worker(monkeypatch, board)
    assert _park(kt, out["request_id"])["ok"]
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        ksr.drain(conn)
        assert kb.get_task(conn, board["requester"]).status == "ready"
        assert ksr.get_request(conn, out["request_id"])["requester_woken"] == 1
        wakes = [k for k, _ in _events(conn, board["requester"]) if k == "supervisor_request_wake"]
        assert len(wakes) == 1
        ksr.drain(conn)
        assert len([k for k, _ in _events(conn, board["requester"]) if k == "supervisor_request_wake"]) == 1


def test_archived_requester_is_rejected_at_execution(board, monkeypatch):
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        kb.archive_task(conn, board["requester"])
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "rejected")]
        assert (board["qa119"], board["dev116"]) in _edges(conn)


def test_unlink_applies_after_requester_completes(board, monkeypatch):
    """Completing the card does not cancel a filed request; the receipt does not reopen the card."""
    out = _tool({"ops": _reorder(board)})
    with kbc.connect() as conn:
        assert kb.complete_task(conn, board["requester"], summary="filed", expected_run_id=board["run_id"])
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "done")]
        assert (board["qa119"], board["dev116"]) not in _edges(conn)
        assert (board["dev67"], board["dev81"]) not in _edges(conn)
        req = ksr.get_request(conn, out["request_id"])
        assert req["error"] is None and req["requester_woken"] == 1
        kinds = [k for k, _ in _events(conn, board["requester"])]
        assert kinds[-2:] == ["supervisor_request_accepted", "supervisor_request_done"]
        assert kb.get_task(conn, board["requester"]).status == "done"


def test_a_request_that_keeps_crashing_executors_is_failed_not_redelivered_forever(board, monkeypatch):
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE supervisor_requests SET status='accepted', executor='otherhost:1:x', "
                         "lease_expires=1, attempts=? WHERE id=?", (ksr.MAX_ATTEMPTS, out["request_id"]))
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "failed")]
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "failed" and "abandoned" in req["error"]
        assert (board["qa119"], board["dev116"]) in _edges(conn)
        assert kb.get_task(conn, board["requester"]).status == "running"


def test_a_leaked_worker_env_in_the_dispatcher_is_logged(board, caplog, monkeypatch):
    _tool({"ops": _reorder(board)})
    monkeypatch.setattr(ksr, "_skip_logged", False)
    with kbc.connect() as conn, caplog.at_level("WARNING", logger=ksr.__name__):
        assert ksr.dispatcher_drain(conn) == []
    assert "not draining supervisor requests" in caplog.text


def test_codex_runtime_workers_can_reach_the_tool():
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
    assert "kanban_supervisor_request" in EXPOSED_TOOLS


def test_a_late_result_does_not_wake_an_unrelated_park(board, monkeypatch):
    from tools import kanban_tools as kt
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        ksr.drain(conn)
    _as_worker(monkeypatch, board)
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    assert json.loads(kt._handle_schedule({"reason": f"SCHEDULED_UNTIL={until} waiting on CI",
                                           "awaiting": {"kind": "clock", "why": "the CI window"}}))["ok"]
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        ksr.drain(conn)
        assert kb.get_task(conn, board["requester"]).status == "scheduled"
        assert ksr.get_request(conn, out["request_id"])["requester_woken"] == 1  # settled, not retried


def test_a_batch_with_any_link_op_is_refused_up_front(board):
    b = board
    out = _tool({"ops": [{"op": "link", "parent_id": b["qa119"], "child_id": b["dev116"]},  # already present
                         {"op": "unlink", "parent_id": b["dev67"], "child_id": b["dev81"]}]})
    assert out["code"] == "link_not_requested", out
    with kbc.connect() as conn:
        assert ksr.list_requests(conn) == []


def _age(conn, rid, seconds):
    with kb.write_txn(conn):
        conn.execute("UPDATE supervisor_requests SET created_at = ? WHERE id = ?", (int(time.time()) - seconds, rid))


def test_a_request_pending_past_the_deadline_is_rejected_as_expired_at_accept(board, monkeypatch):
    """No executor for over a day: the request is not run late; the requester gets a rejection receipt."""
    from tools import kanban_tools as kt
    out = _tool({"ops": _reorder(board)})
    assert _park(kt, out["request_id"])["ok"]
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        _age(conn, out["request_id"], 24 * 3600 + 60)
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "rejected")]
        assert (board["qa119"], board["dev116"]) in _edges(conn)  # target not mutated
        assert (board["dev67"], board["dev81"]) in _edges(conn)
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "rejected" and req["error"].startswith("expired: pending > ")
        assert [r["outcome"] for r in req["result"]] == ["not_applied"] * 2
        kinds = [k for k, _ in _events(conn, board["requester"])]
        assert kinds == ["supervisor_request_queued", "supervisor_request_rejected", "supervisor_request_wake"]
        comments = [c for c in kb.list_comments(conn, board["requester"]) if c.author == ksr.COMMENT_AUTHOR]
        assert len(comments) == 1 and "expired" in comments[0].body
        assert ksr.drain(conn)["processed"] == []  # settled, not retried
        assert ksr._expire(conn, out["request_id"], req["executor"], "me:1:x") is False  # terminal: not re-taken


def test_a_request_pending_just_under_the_deadline_still_runs(board, monkeypatch):
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        _age(conn, out["request_id"], 24 * 3600 - 600)
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "done")]
        assert (board["qa119"], board["dev116"]) not in _edges(conn)


def test_an_old_accepted_request_is_redelivered_not_expired(board, monkeypatch):
    """Only a never-accepted request expires: one an executor took (and then lost) is finished by redelivery."""
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        _age(conn, out["request_id"], 3 * 24 * 3600)
        with kb.write_txn(conn):
            conn.execute("UPDATE supervisor_requests SET status='accepted', executor='otherhost:1:x', "
                         "lease_expires=1, attempts=1 WHERE id=?", (out["request_id"],))
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "done")]


def test_an_expiry_that_crashed_before_its_receipt_is_expired_again(board, monkeypatch):
    """_expire takes the row (accepted, attempts still 0) then dies: the next drain expires it, never runs it."""
    out = _tool({"ops": _reorder(board)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        _age(conn, out["request_id"], 2 * 24 * 3600)
        with kb.write_txn(conn):
            conn.execute("UPDATE supervisor_requests SET status='accepted', executor='otherhost:1:x' "
                         "WHERE id=?", (out["request_id"],))
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "rejected")]
        assert (board["qa119"], board["dev116"]) in _edges(conn)
        assert ksr.get_request(conn, out["request_id"])["error"].startswith("expired: ")


def test_expiry_loses_the_row_to_a_peer_that_took_it_first(board, monkeypatch):
    """_expire's compare-and-swap: a holder change or an acceptance since the scan keeps the row untouched."""
    out = _tool({"ops": _reorder(board)})
    rid = out["request_id"]
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        _age(conn, rid, 2 * 24 * 3600)
        with kb.write_txn(conn):  # another expirer took it after our scan (attempts still 0)
            conn.execute("UPDATE supervisor_requests SET status='accepted', executor='peer:1:x' WHERE id=?", (rid,))
        assert ksr._expire(conn, rid, None, "me:1:x") is False
        with kb.write_txn(conn):  # a peer accepted it to run (attempts 1), seen by us as unheld
            conn.execute("UPDATE supervisor_requests SET status='pending', executor=NULL, attempts=1 "
                         "WHERE id=?", (rid,))
        assert ksr._expire(conn, rid, None, "me:1:x") is False
        req = ksr.get_request(conn, rid)
        assert req["status"] == "pending" and req["error"] is None
