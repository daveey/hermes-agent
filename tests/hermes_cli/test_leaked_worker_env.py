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
    assert "reopen" in msg


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

