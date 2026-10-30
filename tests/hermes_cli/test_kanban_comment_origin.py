"""Cross-gateway replies: comments carry ``origin_gateway`` and parks never filter on the author. [daveey]

Regression for t_01a17f81686694ec (self-heal desktop-00c4a3bda2): the card parked on a probe counting
comments with ``author='macbook'``; the Mac's reply arrived through the hermes-hub relay as author
``hub:gw:macbook``, so the probe never passed and the card sat parked 38 h until David typed "resume".

(a) a park whose wake_check filters ``task_comments`` by author is refused (``author_probe``) and the
    error names ``hermes kanban wait-comment``; ``author !=`` (skip my own notes) still parks;
(b) ``hermes kanban wait-comment --from-gateway macbook`` passes for a relay comment ``hub:gw:macbook``
    and for ``macbook``, not for ``default``; ``comment --origin-gateway`` stores the column;
(c) the migration backfills ``origin_gateway`` from relay/``<gw>/t_...`` authors on a legacy board;
(d) ``wake_probe_stale``: a parked card whose linked hermes-hub request is answered (owner card done)
    while its probe still exits 1 wakes once with a comment.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

INCIDENT_PROBE = (
    "python3 -c \"import sqlite3,os,sys;c=sqlite3.connect('file:'+os.path.expanduser('~/.hermes/kanban.db')"
    "+'?mode=ro',uri=True);sys.exit(0 if c.execute(\\\"select count(*) from task_comments where "
    "task_id='{tid}' and author='macbook'\\\").fetchone()[0]>=2 else 1)\"")
INCIDENT_REASON = ("SCHEDULED_UNTIL=2099-11-01T00:00:00Z — fix shipped; waiting for the Mac read-only verification "
                   "(hermes-hub request t01a17f81-mac-verify) to post a second `macbook` comment here.")
MAC = {"kind": "external", "ref": "macbook gateway", "why": "hub request t01a17f81-mac-verify asks it to reply"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "no-mail.json"))
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()


def _running(conn) -> str:
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _as_worker(monkeypatch, conn, tid):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))


def _tool(name, args):
    from tools import kanban_tools as kt
    return json.loads(getattr(kt, f"_handle_{name}")(args))


def _cli():
    from hermes_cli import kanban as kcli
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    return lambda *argv: kcli.kanban_command(parser.parse_args(["kanban", *argv]))


# -- (a) park guard ----------------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["schedule", "block", "continue"])
def test_park_on_an_author_probe_is_refused(kanban_home, monkeypatch, op) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        probe = INCIDENT_PROBE.replace("{tid}", tid)
        args = {"schedule": {"reason": INCIDENT_REASON, "wake_check": probe, "awaiting": MAC},
                "block": {"reason": INCIDENT_REASON, "kind": "time", "wake_check": probe, "awaiting": MAC},
                "continue": {"checkpoint": INCIDENT_REASON, "resume_after_seconds": 86400, "wake_check": probe,
                             "awaiting": MAC}}[op]
        out = _tool(op, args)
        assert "author_probe" in (out.get("code"), out.get("rejection")) or "author_probe" in str(out), out
        assert f"hermes kanban wait-comment {tid}" in out["error"] and "--from-gateway" in out["error"], out
        assert kb.get_task(conn, tid).status == "running" and kwc.get_state(conn, tid) is None


@pytest.mark.parametrize("probe", [
    "sqlite3 ~/.hermes/kanban.db \"select 1 from task_comments where task_id='{tid}' and author like 'daveey-mini%'\" | grep -q 1",
    "sqlite3 ~/.hermes/kanban.db \"SELECT count(*) FROM task_comments WHERE author IN ('macbook','mac')\" | grep -qv '^0$'",
])
def test_author_like_and_in_probes_are_refused_on_the_cli(kanban_home, capsys, probe) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    rc = run("schedule", tid, INCIDENT_REASON, "--wake-check", probe.replace("{tid}", tid),
             "--awaiting", "external:macbook gateway", "--awaiting-why", "hub request asks it to reply")
    assert rc != 0
    assert "author_probe" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"


def test_author_not_equal_own_notes_still_parks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        probe = (f"sqlite3 ~/.hermes/kanban.db \"select count(*) from task_comments where task_id='{tid}' "
                 f"and author != 'default'\" | grep -qv '^0$'")
        out = _tool("schedule", {"reason": INCIDENT_REASON, "wake_check": probe, "awaiting": MAC})
        assert out.get("status") == "scheduled", out


def test_wait_comment_probe_parks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": INCIDENT_REASON, "awaiting": MAC,
                                 "wake_check": f"hermes kanban wait-comment {tid} --after-id 0 --from-gateway macbook"})
        assert out.get("status") == "scheduled", out


def test_guard_can_be_switched_off(kanban_home, monkeypatch) -> None:
    # The documented undo: ``kanban.author_probe_guard: false`` in config.yaml.
    (kanban_home / "config.yaml").write_text("kanban:\n  author_probe_guard: false\n")
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": INCIDENT_REASON, "awaiting": MAC,
                                 "wake_check": INCIDENT_PROBE.replace("{tid}", tid)})
        assert out.get("status") == "scheduled", out


# -- (b) wait-comment + origin_gateway ---------------------------------------------------------------

@pytest.mark.parametrize("author,origin,want", [
    ("hub:gw:macbook", None, 0),       # the hub relay (the 10-27 reply)
    ("macbook", None, 0),              # a free-form --author
    ("macbook/t_01a18013d59457e9", None, 0),
    ("hub:gw:macbook", "macbook", 0),
    ("default", None, 1),              # the card's own worker
    ("default", "hermes-hub", 1),
])
def test_wait_comment_matches_any_author_spelling_of_the_gateway(kanban_home, capsys, author, origin, want) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.add_comment(conn, tid, author, "verification: fix live", origin_gateway=origin)
    assert run("wait-comment", tid, "--after-id", "0", "--from-gateway", "macbook") == want
    capsys.readouterr()


def test_wait_comment_after_id_and_contains(kanban_home, capsys) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        first = kb.add_comment(conn, tid, "hub:gw:macbook", "evidence: row lacked connection_id")
    assert run("wait-comment", tid, "--after-id", str(first), "--from-gateway", "macbook") == 1
    with kbc.connect_closing() as conn:
        second = kb.add_comment(conn, tid, "hub:gw:macbook", "VERIFIED: deploy live")
    assert run("wait-comment", tid, "--after-id", str(first), "--from-gateway", "macbook") == 0
    out = capsys.readouterr().out
    assert f"comment {second}" in out and "hub:gw:macbook" in out
    assert run("wait-comment", tid, "--after-id", str(first), "--contains", "verified") == 0
    assert run("wait-comment", tid, "--after-id", str(first), "--contains", "rollback") == 1
    assert run("wait-comment", "t_nope", "--after-id", "0") == 2


def test_comment_cli_stores_origin_gateway(kanban_home, capsys) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
    assert run("comment", tid, "--author", "hub:gw:macbook", "--origin-gateway", "macbook", "hello") == 0
    assert run("comment", tid, "--author", "hub:gw:daveey-mini", "relay") == 0
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT author, origin_gateway FROM task_comments WHERE task_id = ? ORDER BY id",
                            (tid,)).fetchall()
    assert [tuple(r) for r in rows] == [("hub:gw:macbook", "macbook"), ("hub:gw:daveey-mini", "daveey-mini")]


# -- (c) backfill ------------------------------------------------------------------------------------

def test_migration_backfills_origin_gateway(kanban_home) -> None:
    db = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
    raw = sqlite3.connect(str(db))
    if "origin_gateway" in {r[1] for r in raw.execute("PRAGMA table_info(task_comments)")}:
        raw.execute("ALTER TABLE task_comments DROP COLUMN origin_gateway")
    for author in ("hub:gw:macbook", "macbook/t_01a18013d59457e9", "default", "hub:op:david"):
        raw.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, 'x', 1)",
                    (tid, author))
    raw.commit()
    raw.close()
    kb.init_db()
    with kbc.connect_closing() as conn:
        got = {r[0]: r[1] for r in conn.execute("SELECT author, origin_gateway FROM task_comments")}
    assert got == {"hub:gw:macbook": "macbook", "macbook/t_01a18013d59457e9": "macbook", "default": None,
                   "hub:op:david": None}


# -- (d) wake_probe_stale ----------------------------------------------------------------------------

def _hub(tmp_path: Path, owner_status: str) -> None:
    state = tmp_path / "hub-state"
    state.mkdir(exist_ok=True)
    db = sqlite3.connect(str(state / "hub.db"))
    db.executescript(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);"
        "CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, "
        "created_at REAL, done_at REAL, posted INTEGER DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,"
        " updated_at INTEGER, PRIMARY KEY (gw, coll, key));")
    db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?, 'done', ?, 1, 2, 0)", (
        "t01a17f81-mac-verify",
        json.dumps({"id": "t01a17f81-mac-verify", "to_gw": "macbook", "coll": "tasks", "key": "default/default/new",
                    "action": "create"}),
        json.dumps({"output": json.dumps({"id": "t_01a18013d59457e9"}), "task": "t_01a18013d59457e9"})))
    db.execute("INSERT OR REPLACE INTO replica VALUES ('macbook', 'tasks', 'default/default/t_01a18013d59457e9', 1, 1,"
               " ?, 1)", (json.dumps({"id": "t_01a18013d59457e9", "status": owner_status}),))
    db.commit()
    db.close()


def _park_on_stuck_probe(monkeypatch, conn) -> str:
    tid = _running(conn)
    _as_worker(monkeypatch, conn, tid)
    out = _tool("schedule", {"reason": INCIDENT_REASON, "wake_check": "exit 1", "check_every": 300, "awaiting": MAC})
    assert out.get("status") == "scheduled", out
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    return tid


def test_answered_hub_request_wakes_a_card_whose_probe_still_fails(kanban_home, tmp_path, monkeypatch) -> None:
    _hub(tmp_path, "done")
    with kbc.connect_closing() as conn:
        tid = _park_on_stuck_probe(monkeypatch, conn)
        kwc.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        task = kb.get_task(conn, tid)
        assert task.status == "ready", task.status
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "wake_probe_stale"]
        assert len(ev) == 1 and ev[0].payload["request"] == "t01a17f81-mac-verify", ev
        note = kb.list_comments(conn, tid)[-1]
        assert "t01a17f81-mac-verify" in note.body and "wake_check" in note.body
        # Once: re-parked on the same request and probe, it stays parked.
        _as_worker(monkeypatch, conn, tid) if kb.claim_task(conn, tid, claimer="worker") else None
        out = _tool("schedule", {"reason": INCIDENT_REASON, "wake_check": "exit 1", "check_every": 300,
                                 "awaiting": MAC})
        assert out.get("status") == "scheduled", out
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 10_000, wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"


def test_pending_hub_request_keeps_the_card_parked(kanban_home, tmp_path, monkeypatch) -> None:
    _hub(tmp_path, "running")
    with kbc.connect_closing() as conn:
        tid = _park_on_stuck_probe(monkeypatch, conn)
        kwc.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "wake_probe_stale"]
