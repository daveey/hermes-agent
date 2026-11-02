"""kanban_* tool fence for a worker's subprocesses follows the board, like the DB fence. [daveey]

A Kanban worker's terminal/execute_code subprocesses inherit ``HERMES_DELEGATED_CHILD_CONTEXT=<board
root>``. The DB fence (``kanban_path_is_fenced``) refuses writes only to the pinned board and boards under
that root, so a throwaway ``HERMES_HOME`` outside it is writable. The tool layer used to refuse every
mutation once the marker was set, so a worker could never show a reviewer a live ``kanban_link`` /
``kanban_block`` refusal on a scratch board and handed "run this command" back to the user
(defect agent-cbb2687c75, t_01a194ea280f2a75).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FOREIGN = "t_01a1937c927e0342"
REFUSAL = "delegate_task child agents are not Kanban run owners"


def _board_with_claimed_task(home: Path) -> tuple[str, int | None]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="scratch probe", assignee="scratch-worker")
        kb.claim_task(conn, tid)
        return tid, kb._current_run_id(conn, tid)


def _snap(tid: str) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        return {"status": task.status, "run": kb._current_run_id(conn, tid), "parents": kb.parent_ids(conn, tid)}


@pytest.fixture
def fenced_layout(monkeypatch, tmp_path):
    """A fenced real root (the marker value) and a scratch home outside it."""
    real_root = tmp_path / "real-hermes"
    scratch = tmp_path / "kanban-probe"
    real_root.mkdir()
    scratch.mkdir()
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.setenv("HERMES_PROFILE", "scratch-worker")
    tid, run_id = _board_with_claimed_task(scratch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return real_root, scratch, tid


def _probe(tid: str) -> tuple[dict, dict, dict, dict]:
    """(a) link a foreign parent, (b) dependency-block on a raw dangling edge; card before/after each."""
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    after_link = _snap(tid)
    assert after_link == before
    with kbc.connect() as conn, kbc.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (FOREIGN, tid))
    before_block = _snap(tid)
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "wait for remote owner"}))
    return link, block, before_block, _snap(tid)


def test_env_fenced_subprocess_reaches_real_logic_on_scratch_home(monkeypatch, fenced_layout):
    """(a) Marker set, HERMES_HOME outside its root: the handlers run their real validation."""
    real_root, _scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))

    link, block, before, after = _probe(tid)

    assert REFUSAL not in json.dumps(link)
    assert "unknown task(s)" in link["error"] and FOREIGN in link["error"]
    assert REFUSAL not in json.dumps(block)
    assert "unknown task(s)" in block["error"] and "awaiting={kind:'card'" in block["error"]
    assert after == before and after["status"] == "running"


def test_env_fenced_subprocess_still_refused_on_board_under_root(monkeypatch, fenced_layout):
    """(b) Control: the same subprocess whose board lies under the fenced root is refused."""
    _real_root, scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(scratch))  # scratch IS the fenced root here
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "x"}))
    comment = json.loads(kt._handle_comment({"task_id": tid, "body": "x"}))
    for out in (link, block, comment):
        assert REFUSAL in out["error"]
        assert "mktemp -d ~/kanban-probe." in out["error"]  # the refusal names the scratch-home route
    assert _snap(tid) == before


def test_env_fenced_subprocess_refused_when_home_is_fenced_but_board_is_not(monkeypatch, fenced_layout):
    """Control: board moved out via HERMES_KANBAN_HOME while HERMES_HOME (gateway config, credentials) is
    still the real, fenced home: refused, so a scratch board never runs with the real home's routes."""
    real_root, scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(scratch))
    monkeypatch.setenv("HERMES_HOME", str(real_root))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    assert REFUSAL in link["error"]
    assert _snap(tid) == before


def test_scratch_home_scope_covers_local_writes_only(monkeypatch, fenced_layout):
    """On a scratch home: comment and an explicitly local create work; a create with any other placement,
    archive (may relay home) and origin adoption stay refused."""
    real_root, _scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    monkeypatch.delenv("HERMES_KANBAN_TASK")  # act as an orchestrator on the scratch board
    from tools import kanban_tools as kt

    assert json.loads(kt._handle_comment({"task_id": tid, "body": "probe"}))["ok"] is True
    created = json.loads(kt._handle_create({"title": "probe child", "assignee": "x", "placement": "local",
                                            "requires": []}))
    assert created.get("ok") is True, created
    for out in (kt._handle_create({"title": "t", "assignee": "x", "placement": "hub", "requires": []}),
                kt._handle_create({"title": "t", "assignee": "x", "requires": []}),
                kt._handle_archive({"task_id": created["task_id"]})):
        assert REFUSAL in json.loads(out)["error"], out


def test_env_fenced_subprocess_still_refused_on_pinned_db(monkeypatch, fenced_layout):
    """(b) Control: the dispatcher-pinned HERMES_KANBAN_DB is refused even when it sits outside the root."""
    real_root, _scratch, tid = fenced_layout
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "x", "board": "other"}))
    assert REFUSAL in link["error"] and REFUSAL in block["error"]
    assert _snap(tid) == before


def test_legacy_marker_still_refuses_everywhere(monkeypatch, fenced_layout):
    _real_root, _scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    from tools import kanban_tools as kt

    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]


def test_in_process_delegate_child_still_refused_on_scratch_home(fenced_layout):
    """(c) Control: an in-process delegate_task child is refused on any board, scratch included."""
    _real_root, _scratch, tid = fenced_layout
    from agent.delegation_context import delegated_child_context
    from tools import kanban_tools as kt

    before = _snap(tid)
    with delegated_child_context("child-session"):
        link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
        comment = json.loads(kt._handle_comment({"task_id": tid, "body": "x"}))
    assert REFUSAL in link["error"] and REFUSAL in comment["error"]
    assert _snap(tid) == before


def test_real_worker_subprocess_drives_tools_on_scratch_home(monkeypatch, tmp_path):
    """End to end: a real subprocess with the env a worker's terminal gets (``delegated_child_subprocess_env``,
    owner-bound marker) and a scratch HERMES_HOME outside the fenced root gets the real refusal text, while the
    same subprocess on a board under the root is still refused by the tool fence."""
    from agent.delegation_context import delegated_child_subprocess_env

    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    real_root = tmp_path / "real-hermes"
    homes = {"scratch": tmp_path / "kanban-probe", "fenced": real_root / "sub"}
    tasks = {}
    for name, home in homes.items():  # boards created by the (unfenced) owner, as the dispatcher would
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        tasks[name] = _board_with_claimed_task(home)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_KANBAN", "HERMES_DELEGATED"))}
    base["HERMES_DELEGATED_CHILD_CONTEXT"] = str(real_root)
    env = delegated_child_subprocess_env(base)
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == str(real_root) and env.get("HERMES_DELEGATED_CHILD_OWNER")
    script = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from tools import kanban_tools as kt
        tid = os.environ["HERMES_KANBAN_TASK"]
        print(json.dumps(json.loads(kt._handle_link({{"parent_id": {FOREIGN!r}, "child_id": tid}}))))
    """)
    outputs = {}
    for name, home in homes.items():
        tid, run = tasks[name]
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False,
            env={**env, "HERMES_HOME": str(home), "HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(run)})
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        outputs[name] = lines[-1] if proc.returncode == 0 and lines else proc.stderr[-2000:]
    assert "unknown task(s)" in outputs["scratch"] and REFUSAL not in outputs["scratch"], outputs["scratch"]
    assert REFUSAL in outputs["fenced"], outputs["fenced"]
    for name, home in homes.items():
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert _snap(tasks[name][0])["parents"] == []


def test_descendant_refusal_text_names_the_scratch_home_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(tmp_path))
    from hermes_cli.kanban_db_fence import descendant_refusal_text

    text = descendant_refusal_text()
    assert "HERMES_HOME" in text and "mktemp -d ~/kanban-probe." in text and "rm -rf" in text
