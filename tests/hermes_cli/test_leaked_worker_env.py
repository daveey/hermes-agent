"""Leaked kanban-worker env in top-level launches (Hermes.app pid 59331, 2026-10-21). [daveey]

A worker's ``hermes update`` reopened Hermes Desktop with the worker's env: the delegation marker,
the kanban pins AND the worker's session identity (HERMES_SESSION_ID, HERMES_SESSION_SOURCE=kanban,
HERMES_SINGLE_QUERY_SESSION=1, TERMINAL_CWD=<worker workspace>). Every Desktop chat then refused
Kanban writes with an error that never said why.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import top_level_env as tle

MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

# Exactly the HERMES_* identity keys pid 59331 carried (minus config-derived TERMINAL_* exports).
PID_59331 = {
    MARKER: "/Users/x/.hermes",
    "HERMES_SESSION_ID": "20261021_185223_3c48bc",
    "HERMES_SESSION_SOURCE": "kanban",
    "HERMES_SINGLE_QUERY_SESSION": "1",
    "HERMES_KANBAN_BOARD": "default",
    "HERMES_KANBAN_DB": "/Users/x/.hermes/kanban.db",
    "HERMES_KANBAN_WORKSPACE": "/Users/x/.hermes/kanban/workspaces/t_45d5a361",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/Users/x/.hermes/kanban/workspaces",
    "TERMINAL_CWD": "/Users/x/.hermes/kanban/workspaces/t_45d5a361",
}
KEEP = {"PATH": "/bin", "HERMES_HOME": "/Users/x/.hermes", "HERMES_PROFILE": "default"}


# ── scrub: keys the shared agent-scope list (9778500b6b) still missed ──────────────────────

def test_scrub_drops_every_identity_key_pid_59331_carried():
    env = {**KEEP, **PID_59331, "TERMINAL_TIMEOUT": "180"}
    tle.scrub_leaked_worker_env(env)
    assert env == {**KEEP, "TERMINAL_TIMEOUT": "180"}


def test_scrub_keeps_terminal_cwd_without_a_worker_marker():
    env = {"TERMINAL_CWD": "/tmp", "HERMES_UI_SESSION_ID": "u"}
    assert tle.scrub_leaked_worker_env(env) == []
    assert env == {"TERMINAL_CWD": "/tmp", "HERMES_UI_SESSION_ID": "u"}


def test_agent_scope_covers_gateway_session_context():
    """Every per-session routing var the gateway binds is worker identity when inherited."""
    from agent.delegation_context import is_agent_scope_env_key
    from gateway.session_context import _VAR_MAP
    assert [k for k in _VAR_MAP if not is_agent_scope_env_key(k)] == []


@pytest.mark.platforms("macos")
def test_desktop_relaunch_after_update_drops_session_identity(monkeypatch, tmp_path):
    """Launch path 1: ``hermes update``'s post-rebuild reopen (``_relaunch_stopped_desktops``)."""
    from hermes_cli import main_desktop as md
    for k, v in PID_59331.items():
        monkeypatch.setenv(k, v)
    exe = tmp_path / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(md, "_desktop_packaged_executable", lambda d: exe)
    monkeypatch.setattr(md, "_desktop_relaunch_after_rebuild_enabled", lambda: True)
    monkeypatch.setattr(md.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(md.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    md._relaunch_stopped_desktops(tmp_path, [[]])
    env = calls[0][1]["env"]
    assert not (set(PID_59331) & set(env))


@pytest.mark.parametrize("command", ["serve", "dashboard", "desktop", "gateway", "gui"])
def test_main_entry_scrubs_session_identity_for_every_top_level_surface(command):
    """Launch path 2: ``hermes <surface>`` started from a worker shell (real subprocess, real main())."""
    probe = (f"import os, sys, json; sys.argv=['hermes', {command!r}]; "
             "from hermes_cli.top_level_env import scrub_for_top_level_launch as s; "
             f"s(sys.argv[1:]); print(json.dumps(sorted(k for k in {sorted(PID_59331)!r} if k in os.environ)))")
    env = {**os.environ, **PID_59331}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [os.getcwd(), env.get("PYTHONPATH", "")]))
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                         timeout=60, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
    assert "dropped inherited agent-scope env" in out.stderr


# ── detection: a delegation marker with no live parent agent ─────────────────────────────────

def _board(tmp_path, rows):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, worker_pid INTEGER)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def _leak_env(db, task="t_45d5a361"):
    return {MARKER: str(db.parent), "HERMES_KANBAN_DB": str(db),
            "HERMES_KANBAN_WORKSPACE": f"/w/kanban/workspaces/{task}"}


def test_diagnosis_names_a_finished_worker_card(tmp_path):
    db = _board(tmp_path, [("t_45d5a361", "done", 99999999)])
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and "leaked worker env" in msg and "t_45d5a361" in msg and "is done" in msg
    assert "reopen" in msg and "Cmd-Q" in msg


def test_diagnosis_names_a_dead_worker_pid(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    db = _board(tmp_path, [("t_45d5a361", "running", p.pid)])
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and f"worker pid {p.pid} is gone" in msg


def test_diagnosis_flags_a_live_worker_this_process_does_not_descend_from(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        db = _board(tmp_path, [("t_45d5a361", "running", p.pid)])
        msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
        assert msg and "does not descend from" in msg
    finally:
        p.kill()
        p.wait()


def test_no_diagnosis_for_a_real_child_of_a_live_worker(tmp_path):
    """The worker is this process's own ancestor (here: this process): a real delegate child."""
    for i, pid in enumerate((os.getppid(), os.getpid())):
        (tmp_path / str(i)).mkdir()
        db = _board(tmp_path / str(i), [("t_45d5a361", "running", pid)])
        assert tle.leaked_worker_env_diagnosis(_leak_env(db)) is None


def test_backend_startup_warning_names_the_dead_lineage(monkeypatch, tmp_path, caplog):
    """Detection at launch: ``hermes serve`` started with a marker whose worker is gone logs why."""
    db = _board(tmp_path, [("t_45d5a361", "done", None)])
    for k, v in _leak_env(db).items():
        monkeypatch.setenv(k, v)
    with caplog.at_level("WARNING", logger="hermes_cli.top_level_env"):
        tle.scrub_for_top_level_launch(["serve"])
    assert "leaked worker env" in caplog.text and "t_45d5a361 is done" in caplog.text
    assert MARKER not in os.environ


@pytest.mark.parametrize("env_patch", [
    {MARKER: ""},                                       # no marker at all
    {"HERMES_KANBAN_WORKSPACE": "/tmp/not-a-task"},     # lineage task unknown
    {"HERMES_KANBAN_DB": "/nonexistent/kanban.db"},     # board unreadable
])
def test_diagnosis_is_conservative(tmp_path, env_patch):
    db = _board(tmp_path, [("t_45d5a361", "done", None)])
    assert tle.leaked_worker_env_diagnosis({**_leak_env(db), **env_patch}) is None


# ── refusal surfaces name the leak ───────────────────────────────────────────────────────────

@pytest.fixture
def leaked_desktop(monkeypatch, tmp_path):
    """A process shaped like pid 59331's backend: marker + pins of a finished card, real board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="old worker", assignee="default")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done")
        other = kb.create_task(conn, title="orchestrator wants to close this", assignee="default")
    finally:
        conn.close()
    monkeypatch.setenv(MARKER, str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(home / "kanban" / "workspaces" / tid))
    return kb, kbc, tid, other


def test_db_fence_names_the_leak(leaked_desktop):
    kb, kbc, tid, other = leaked_desktop
    conn = kbc.connect()
    try:
        with pytest.raises(PermissionError) as exc:
            kb.complete_task(conn, other, summary="x")
    finally:
        conn.close()
    assert "cannot mutate Kanban" in str(exc.value)
    assert "leaked worker env" in str(exc.value) and tid in str(exc.value)


def test_tool_refusal_names_the_leak(leaked_desktop):
    _kb, _kbc, tid, other = leaked_desktop
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_comment({"task_id": other, "body": "hi"}))
    text = json.dumps(out)
    assert "not Kanban run owners" in text and "leaked worker env" in text and tid in text


def test_cli_refusal_names_the_leak(leaked_desktop, capsys):
    _kb, _kbc, tid, other = leaked_desktop
    import argparse
    from hermes_cli import kanban as kcli
    rc = kcli.kanban_command(argparse.Namespace(kanban_action="archive", task_ids=[other], board=None))
    err = capsys.readouterr()
    assert rc != 0
    text = err.err + err.out
    assert "cannot mutate Kanban tasks via the CLI" in text and "leaked worker env" in text and tid in text



# ── a live worker whose card was parked by kanban_block is not a leak ──────────────────────────

def _parked_board(tmp_path, status, run_pid):
    """A card parked by kanban_block: card pid cleared, latest run keeps its worker pid."""
    db = _board(tmp_path, [("t_45d5a361", status, None)])
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, worker_pid INTEGER)")
    conn.execute("INSERT INTO task_runs (task_id, worker_pid) VALUES ('t_45d5a361', 99999999)")  # older run
    conn.execute("INSERT INTO task_runs (task_id, worker_pid) VALUES ('t_45d5a361', ?)", (run_pid,))
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("status", ["todo", "blocked", "scheduled"])
def test_no_diagnosis_for_a_live_worker_whose_card_was_parked(tmp_path, status):
    """kanban_block(kind=dependency) moves the card to todo while the worker keeps running."""
    db = _parked_board(tmp_path, status, os.getpid())
    assert tle.leaked_worker_env_diagnosis(_leak_env(db)) is None


def test_parked_card_whose_worker_pid_is_gone_is_still_a_leak(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    db = _parked_board(tmp_path, "todo", p.pid)
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and f"worker pid {p.pid} is gone" in msg


def test_parked_card_with_a_foreign_live_pid_is_still_a_leak(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        db = _parked_board(tmp_path, "todo", p.pid)
        msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
        assert msg and "does not descend from" in msg
    finally:
        p.kill()
        p.wait()


def test_parked_card_without_any_run_pid_is_still_a_leak(tmp_path):
    db = _board(tmp_path, [("t_45d5a361", "todo", None)])
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and "is todo" in msg


# ── a live managed worker: the shell descends from the worker_bound interpreter ───────────────
# t_01a18b6ffd5e33c1 (hub, 2026-10-30): the worker's own shell (parent: ``agent.managed_worker``
# pid 4081359, the run's ``worker_bound`` pid and HERMES_DELEGATED_CHILD_OWNER) was told
# "leaked worker env ... Cmd-Q", because only task_runs.worker_pid (4081264, the
# kanban_worker_client spawn pid, a sibling under the gateway) was checked for ancestry.

def _managed_board(tmp_path, spawn_pid, bound_pid, status="running"):
    db = _board(tmp_path, [("t_45d5a361", status, spawn_pid)])
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, worker_pid INTEGER)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, "
                 "kind TEXT, payload TEXT, created_at INTEGER)")
    conn.execute("INSERT INTO task_runs (task_id, worker_pid) VALUES ('t_45d5a361', ?)", (spawn_pid,))
    run_id = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
    # An older run's bind must not vouch for the current run.
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                 "VALUES ('t_45d5a361', ?, 'worker_bound', ?, 0)", (run_id - 1, json.dumps({"pid": os.getpid()})))
    if bound_pid is not None:
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES ('t_45d5a361', ?, 'worker_bound', ?, 1)", (run_id, json.dumps({"pid": bound_pid})))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def sibling_spawn_pid():
    """A live process this one does not descend from (the kanban_worker_client spawn pid)."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


def test_no_diagnosis_for_a_shell_of_the_bound_managed_worker(tmp_path, sibling_spawn_pid):
    db = _managed_board(tmp_path, sibling_spawn_pid, os.getpid())
    assert tle.leaked_worker_env_diagnosis(_leak_env(db)) is None


def test_no_diagnosis_when_the_delegation_owner_is_a_live_ancestor(tmp_path, sibling_spawn_pid):
    """No worker_bound row readable, but HERMES_DELEGATED_CHILD_OWNER names a live ancestor."""
    db = _managed_board(tmp_path, sibling_spawn_pid, None)
    env = {**_leak_env(db), "HERMES_DELEGATED_CHILD_OWNER": f"{os.getppid()}:123"}
    assert tle.leaked_worker_env_diagnosis(env) is None


def test_bound_pid_this_process_does_not_descend_from_is_still_a_leak(tmp_path, sibling_spawn_pid):
    db = _managed_board(tmp_path, sibling_spawn_pid, sibling_spawn_pid)
    env = {**_leak_env(db), "HERMES_DELEGATED_CHILD_OWNER": f"{sibling_spawn_pid}:123"}
    msg = tle.leaked_worker_env_diagnosis(env)
    assert msg and "does not descend from" in msg


def test_older_runs_bind_does_not_vouch_for_the_current_run(tmp_path, sibling_spawn_pid):
    db = _managed_board(tmp_path, sibling_spawn_pid, None)
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and "does not descend from" in msg


def test_live_managed_worker_shell_gets_the_plain_cli_fence(monkeypatch, tmp_path, capsys, sibling_spawn_pid):
    """End to end on a real board: the fence still refuses, but without the Cmd-Q leak advice."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="live managed worker", assignee="default")
        kb.claim_task(conn, tid)
        other = kb.create_task(conn, title="the worker tries to unblock this", assignee="default")
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        with conn:
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (sibling_spawn_pid, tid))
            conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (sibling_spawn_pid, run_id))
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                         "VALUES (?, ?, 'worker_bound', ?, 0)", (tid, run_id, json.dumps({"pid": os.getpid()})))
    finally:
        conn.close()
    monkeypatch.setenv(MARKER, str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(home / "kanban" / "workspaces" / tid))
    import argparse
    from hermes_cli import kanban as kcli
    rc = kcli.kanban_command(argparse.Namespace(kanban_action="archive", task_ids=[other], board=None))
    err = capsys.readouterr()
    text = err.err + err.out
    assert rc != 0
    assert "cannot mutate Kanban tasks via the CLI" in text and "kanban_* tools" in text
    assert "leaked worker env" not in text and "Cmd-Q" not in text


# ── a live worker whose own card is finished is fenced, not leaked ───────────────────────────
# t_01a198dc526bff6e (hub, 2026-11-01): after kanban_complete the worker's own shell ran
# `hermes kanban edit --priority` and was told "leaked worker env ... Hermes Desktop: Cmd-Q".
# No Desktop was involved: the env was the worker's own and its card was simply done.

def _assert_finished_hint(msg, status="done"):
    assert msg and f"t_45d5a361 is {status}" in msg and "kanban_comment" in msg
    assert "Cmd-Q" not in msg and "leaked worker env" not in msg


@pytest.mark.parametrize("status", ["done", "archived"])
def test_finished_card_of_a_live_bound_ancestor_worker_gets_the_fenced_hint(tmp_path, sibling_spawn_pid, status):
    db = _managed_board(tmp_path, sibling_spawn_pid, os.getpid(), status=status)
    _assert_finished_hint(tle.leaked_worker_env_diagnosis(_leak_env(db)), status)


def test_finished_card_whose_run_pid_is_a_live_ancestor_gets_the_fenced_hint(tmp_path):
    """complete_task clears the card's worker_pid; the latest run keeps the worker's pid."""
    db = _parked_board(tmp_path, "done", os.getppid())
    _assert_finished_hint(tle.leaked_worker_env_diagnosis(_leak_env(db)))


def test_finished_card_whose_delegation_owner_is_a_live_ancestor_gets_the_fenced_hint(tmp_path, sibling_spawn_pid):
    db = _managed_board(tmp_path, sibling_spawn_pid, None, status="done")
    env = {**_leak_env(db), "HERMES_DELEGATED_CHILD_OWNER": f"{os.getppid()}:123"}
    _assert_finished_hint(tle.leaked_worker_env_diagnosis(env))


def test_finished_card_with_a_dead_worker_keeps_the_desktop_hint(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    db = _managed_board(tmp_path, p.pid, p.pid, status="done")
    msg = tle.leaked_worker_env_diagnosis(_leak_env(db))
    assert msg and "leaked worker env" in msg and "Cmd-Q" in msg and "kanban_comment" not in msg


def test_finished_card_with_a_live_non_ancestor_worker_keeps_the_desktop_hint(tmp_path, sibling_spawn_pid):
    """A Desktop relaunched from inside a still-running worker process: not its descendant."""
    db = _managed_board(tmp_path, sibling_spawn_pid, sibling_spawn_pid, status="done")
    env = {**_leak_env(db), "HERMES_DELEGATED_CHILD_OWNER": f"{sibling_spawn_pid}:123"}
    msg = tle.leaked_worker_env_diagnosis(env)
    assert msg and "leaked worker env" in msg and "Cmd-Q" in msg and "kanban_comment" not in msg


def test_finished_worker_shell_cli_refusal_points_at_kanban_comment(monkeypatch, tmp_path, capsys):
    """End to end: a real board, a completed card whose bound worker is this process, `kanban archive`."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker that already completed", assignee="default")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        with conn:
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                         "VALUES (?, ?, 'worker_bound', ?, 0)", (tid, run_id, json.dumps({"pid": os.getpid()})))
        kb.complete_task(conn, tid, summary="done")
        other = kb.create_task(conn, title="the finished worker tries to edit this", assignee="default")
    finally:
        conn.close()
    monkeypatch.setenv(MARKER, str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(home / "kanban" / "workspaces" / tid))
    import argparse
    from hermes_cli import kanban as kcli
    rc = kcli.kanban_command(argparse.Namespace(kanban_action="archive", task_ids=[other], board=None))
    err = capsys.readouterr()
    text = err.err + err.out
    assert rc != 0
    assert "cannot mutate Kanban tasks via the CLI" in text
    assert f"{tid} is done" in text and "kanban_comment" in text
    assert "leaked worker env" not in text and "Cmd-Q" not in text
