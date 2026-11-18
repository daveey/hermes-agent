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
        # A pre-column DB also predates the activity triggers, which name every read column and block the DROP.
        from hermes_cli.kanban_activity_revision import drop_triggers
        drop_triggers(raw)
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


# -- (e) comments that arrive through the Board feed -------------------------------------------------
# Regression for t_01a1ccfca36ad3e3 (2026-11-12): parked on `wait-comment --from-gateway <gw>`, the card slept
# through two replies a worker-1 chat wrote as author `default`. The Board row said origin_gateway=worker-1, but
# the pulled copy stored NULL (origin_gateway is machine-local and nothing derived it), so no gateway matched.

@pytest.fixture
def synced_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``kanban_home`` with ``kanban.sync.enabled: true`` (sync tables and capture triggers installed)."""
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "no-mail.json"))
    support.write_sync_config(home, True)
    kb.init_db()
    return home


def _synced_comment(tid: str, author: str) -> dict:
    return {"task_id": tid, "author": author, "body": "PR1165 merged: measure from the next launch",
            "created_at": int(time.time())}


@pytest.mark.parametrize("author,feed_origin,want_origin", [
    ("default", "worker-1", "worker-1"),        # the incident: a profile-named author written on worker-1
    ("hub:gw:macbook", "hub", "macbook"),       # the relay names the real sender, which wins over the writer
    ("default", "", None),                      # no sender on the wire: unknown, never this gateway's name
    ("default", "not a gateway!", None),        # a malformed sender is not stored
])
def test_feed_comment_keeps_the_sending_gateway(synced_home, tmp_path, capsys, author, feed_origin,
                                                want_origin) -> None:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        after = kb.add_comment(conn, tid, "default", "parked")
        client.ensure_identity(conn, "default", "worker-3")
        sync.set_mode(conn, "primary")
        change = sync.RemoteChange(1, "task_comments", "01a1cebe-926b-753b-9c59-8a437dfc7269", "upsert",
                                   _synced_comment(tid, author), 1, "", feed_origin)
        sync.apply_remote_changes(conn, [change], new_cursor=1, observers=False)
        got = conn.execute("SELECT origin_gateway FROM task_comments WHERE uid = ?",
                           ("01a1cebe-926b-753b-9c59-8a437dfc7269",)).fetchone()[0]
    assert got == want_origin
    if want_origin:
        assert _cli()("wait-comment", tid, "--after-id", str(after), "--from-gateway", want_origin) == 0
    capsys.readouterr()


def test_feed_does_not_overwrite_a_local_origin(synced_home) -> None:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        # A plain author re-derives to nothing, so only an overwrite could change the origin.
        cid = kb.add_comment(conn, tid, "default", "relay", origin_gateway="macbook")
        cuid = conn.execute("SELECT uid FROM task_comments WHERE id = ?", (cid,)).fetchone()[0]
        # Pushed and acknowledged: no pending outbox row, so the feed copy reaches the UPDATE path.
        conn.execute("DELETE FROM kanban_sync_outbox WHERE row_uid = ?", (cuid,))
        client.ensure_identity(conn, "default", "worker-3")
        sync.set_mode(conn, "primary")
        change = sync.RemoteChange(1, "task_comments", cuid, "upsert",
                                   _synced_comment(tid, "default"), 2, "", "hub")
        sync.apply_remote_changes(conn, [change], new_cursor=1, observers=False)
        got = conn.execute("SELECT origin_gateway FROM task_comments WHERE id = ?", (cid,)).fetchone()[0]
    assert got == "macbook"


def test_worker_import_carries_the_replica_origin(synced_home, tmp_path) -> None:
    """A card a worker node copies from its replica keeps each comment's sender (kanban_worker_node)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_worker_node as node
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        fields = json.loads(conn.execute("SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' "
                                         "AND row_uid=? ORDER BY rowid LIMIT 1", (tid,)).fetchone()[0])
    fields.update(created_on="worker-1", runs_on=json.dumps(["machine:worker-3"]))
    with kbc.connect(tmp_path / "replica.db") as replica, kbc.connect(tmp_path / "worker.db") as worker:
        client.ensure_identity(replica, "default", "worker-3")
        client.ensure_identity(worker, "default", "worker-3")
        sync.set_mode(worker, "primary")
        sync.apply_remote_changes(replica, [
            sync.RemoteChange(1, "tasks", tid, "upsert", fields, 1, "", "worker-1"),
            sync.RemoteChange(2, "task_comments", "01a1cebe-926b-753b-9c59-8a437dfc7269", "upsert",
                              _synced_comment(tid, "default"), 1, "", "worker-1"),
        ], new_cursor=2, observers=False)
        assert node.import_cards(worker, replica, [tid]) == [tid]
        got = worker.execute("SELECT origin_gateway FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0]
    assert got == "worker-1"


def test_pending_hub_request_keeps_the_card_parked(kanban_home, tmp_path, monkeypatch) -> None:
    _hub(tmp_path, "running")
    with kbc.connect_closing() as conn:
        tid = _park_on_stuck_probe(monkeypatch, conn)
        kwc.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "wake_probe_stale"]


# -- (e) a reply from one card, and telling the sender when a comment will not wake the reader -----

@pytest.mark.parametrize("author,want", [
    ("worker-1/t_01a1a1dea9c64d6a", 0),      # the source card's worker, on any gateway
    ("t_01a1a1dea9c64d6a", 0),               # a bare id
    ("worker-1/t_01a1c412aac138e9", 1),      # another card (a burn-down consumer recheck)
    ("default", 1),
    ("worker-1/t_01a1a1dea9c64d6a-x", 1),    # not a suffix match on a longer token
])
def test_wait_comment_from_card(kanban_home, capsys, author, want) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        kb.add_comment(conn, tid, author, "bounded SOURCE GO REQUEST for PR4151")
    assert run("wait-comment", tid, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a") == want
    assert run("wait-comment", tid, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a",
               "--contains", "rollback") == 1
    capsys.readouterr()


def _park_on(conn, tid: str, cmd: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        conn.execute("INSERT OR REPLACE INTO task_wake_checks(task_id, cmd, every, next_at) VALUES (?,?,900,?)",
                     (tid, cmd, int(time.time()) + 900))


def test_comment_cli_says_when_it_will_not_wake_a_parked_card(kanban_home, capsys) -> None:
    """t_01a194b9de51e620: the request was worded 'bounded SOURCE GO REQUEST'; the probe wanted another phrase."""
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 587534 --contains "
                            f"'BOUNDED exact-head SOURCE GO REQUEST' || hermes kanban wait-comment {tid} "
                            f"--after-id 587534 --contains 'PR4144'")
    assert run("comment", tid, "--author", "worker-1/t_01a1a1dea9c64d6a",
               "Run151 bounded SOURCE GO REQUEST for PR4151") == 0
    out = capsys.readouterr().out
    assert "will NOT wake the card" in out
    assert "BOUNDED exact-head SOURCE GO REQUEST" in out and "PR4144" in out
    # either clause matching is enough, and then nothing is said
    assert run("comment", tid, "--author", "worker-1/t_01a1a1dea9c64d6a", "milestone on pr4144 reached") == 0
    assert "will NOT wake" not in capsys.readouterr().out


def test_comment_wake_note_from_card_and_other_probes(kanban_home, capsys) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        other = kb.create_task(conn, title="other", assignee="worker")
        plain = kb.create_task(conn, title="unparked", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 0 --from-card t_01a1a1dea9c64d6a")
        # a probe that is not wait-comment, or that waits on another card, is never judged
        _park_on(conn, other, f"test -f /tmp/x || hermes kanban wait-comment {tid} --contains nope")
    assert run("comment", tid, "--author", "worker-1/t_01a1c412aac138e9", "consumer recheck") == 0
    assert "from card t_01a1a1dea9c64d6a" in capsys.readouterr().out
    assert run("comment", tid, "--author", "worker-a2/t_01a1a1dea9c64d6a", "any wording at all") == 0
    assert "will NOT wake" not in capsys.readouterr().out
    for card in (other, plain):
        assert run("comment", card, "--author", "default", "hello") == 0
        assert "will NOT wake" not in capsys.readouterr().out


def test_from_card_matches_a_worker_commenting_from_its_own_run(kanban_home, capsys, monkeypatch) -> None:
    """Workers share a profile, so the author is 'default': the commented event's writer_task names the card."""
    run = _cli()
    with kbc.connect_closing() as conn:
        owner = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, owner, f"hermes kanban wait-comment {owner} --after-id 0 --from-card t_01a1a1dea9c64d6a")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_01a1c412aac138e9")      # an unrelated consumer card
    assert run("comment", owner, "--author", "default", "recheck") == 0
    assert "from card t_01a1a1dea9c64d6a" in capsys.readouterr().out
    assert run("wait-comment", owner, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a") == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_01a1a1dea9c64d6a")      # the awaited card's worker
    assert run("comment", owner, "--author", "default", "any wording") == 0
    assert "will NOT wake" not in capsys.readouterr().out
    assert run("wait-comment", owner, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a") == 0
    assert run("wait-comment", owner, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a",
               "--contains", "rollback") == 1
    assert run("wait-comment", owner, "--after-id", "0", "--from-card", "  ") == 1
    capsys.readouterr()


def test_wake_note_agrees_with_the_probe_on_the_local_gateway(kanban_home, capsys, monkeypatch) -> None:
    """A comment with no explicit origin is stored with this gateway: no false 'will not wake'."""
    from hermes_cli import kanban_comment_origin as kco
    monkeypatch.setattr(kco, "_local_gateway", lambda: "mbp")
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 0 --from-gateway mbp")
    assert run("comment", tid, "--author", "zed", "hello") == 0
    assert "will NOT wake" not in capsys.readouterr().out
    assert run("wait-comment", tid, "--after-id", "0", "--from-gateway", "mbp") == 0
    with kbc.connect_closing() as conn:
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 0 --from-gateway macbook")
    assert run("comment", tid, "--author", "zed", "hello again") == 0
    assert "from gateway macbook" in capsys.readouterr().out


@pytest.mark.parametrize("probe", [
    "hermes kanban wait-comment {t} --contains A1 && hermes kanban wait-comment {t} --contains B2",
    "hermes kanban wait-comment {t} --contains A1 ; true",
    "test -f /tmp/x || hermes kanban wait-comment {t} --contains A1",
    "(hermes kanban wait-comment {t} --contains A1)",
    "! hermes kanban wait-comment {t} --contains A1",
    "hermes kanban wait-comment {t} --contains A1 | cat",
    "hermes kanban wait-comment {t} --contains 'unterminated",
    "hermes kanban wait-comment t_other --contains A1",
    "hermes kanban wait-comment {t} --unknown-flag --contains A1",
    "hermes kanban wait-comment {t} --contains PR#42 || hermes kanban wait-comment {t} --contains GO",
    "hermes kanban wait-comment {t} --contains \"$PHRASE\"",
    "hermes kanban wait-comment {t} --contains A1 2>/dev/null",
    "hermes kanban wait-comment {t} --contains A*",
])
def test_a_probe_that_is_not_only_wait_comment_alternatives_is_never_judged(kanban_home, capsys, probe) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, tid, probe.format(t=tid))
    assert run("comment", tid, "--author", "default", "matches nothing") == 0
    assert "will NOT wake" not in capsys.readouterr().out


def test_quoted_separators_and_equals_form_are_read_as_values(kanban_home, capsys) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id=5 --contains 'GO; proceed || now' "
                            f"|| hermes kanban wait-comment {tid} --contains=PR4144")
        ready = kb.create_task(conn, title="not parked", assignee="worker")
        with kb.write_txn(conn):   # a leftover probe row on a card that is not scheduled says nothing
            conn.execute("INSERT INTO task_wake_checks(task_id, cmd, every, next_at) VALUES (?,?,900,0)",
                         (ready, f"hermes kanban wait-comment {ready} --contains nope"))
    assert run("comment", tid, "--author", "default", "go; proceed || now") == 0
    assert "will NOT wake" not in capsys.readouterr().out
    assert run("comment", tid, "--author", "default", "something else") == 0
    out = capsys.readouterr().out
    assert "'GO; proceed || now'" in out and "PR4144" in out
    assert run("comment", ready, "--author", "default", "hello") == 0
    assert "will NOT wake" not in capsys.readouterr().out


def test_no_space_alternatives_and_a_foreign_comment_id_never_matching_a_local_row(kanban_home, capsys) -> None:
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --contains A1||hermes kanban wait-comment {tid} "
                            f"--contains B2")
        local = kb.add_comment(conn, tid, "default", "unrelated local note")
        # A synced event from another board: its comment_id is that board's integer and happens to equal ours.
        kb._append_event(conn, tid, "commented", {"author": "default", "len": 5, "comment_id": local,
                                                   "comment_uid": "u-foreign-77",
                                                   "writer_task": "t_01a1a1dea9c64d6a"})
        kb._append_event(conn, tid, "commented", {"comment_id": "not-a-number", "writer_task": "t_01a1a1dea9c64d6a"})
    assert run("wait-comment", tid, "--after-id", "0", "--from-card", "t_01a1a1dea9c64d6a") == 1
    assert run("comment", tid, "--author", "default", "has b2 in it") == 0
    assert "will NOT wake" not in capsys.readouterr().out
    assert run("comment", tid, "--author", "default", "neither") == 0
    out = capsys.readouterr().out
    assert "'A1'" in out and "'B2'" in out

