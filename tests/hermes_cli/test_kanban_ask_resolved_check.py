"""A human ask can carry ``resolved_check``: a probe that says David's step already happened outside Hermes
(he pressed the device button, flipped the Settings toggle), so the card resumes without his unblock. [daveey]

Entry paths: the kanban_block tool, the kanban_create tool (initial_status=blocked), ``hermes kanban block
--human-request`` (one card or ``--ids``), ``hermes kanban create --initial-status blocked``, ``hermes kanban
block --metadata-only --human-request`` (add/change/remove), and the dashboard's PATCH /tasks/{id} and POST
/tasks/bulk (status=blocked or metadata_only).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_human_request import validate_human_request

ASK = {"action": "Press the pair button on the watch. Unblock = finish pairing; archive = drop the watch.",
       "why_human": "physical button on David's device", "interaction": "physical",
       "scope": "watch pairing", "evidence": "hub shows the watch unpaired"}


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / ".hermes"
    home.mkdir()
    # Own every input to board resolution: a Kanban worker's HERMES_KANBAN_DB pin outranks HERMES_HOME, and a
    # copy of this fixture run outside tests/ (no conftest) wrote three cards to the live board that way.
    for key in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = kb.kanban_db_path().resolve()
    assert db.is_relative_to(home.resolve()), f"board resolved outside the test's home: {db}"
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv('HERMES_SESSION_ID', register_chat(home))
    yield tmp_path
    kwc._shutdown_for_tests()


def _flag(tmp: Path) -> Path:
    return tmp / "paired-flag"


def _probe(flag: Path) -> str:
    return f"test -f {flag} && cat {flag} || exit 1"


def _ask(cmd: str, **extra) -> dict:
    return {**ASK, "resolved_check": cmd, **extra}


def _running(conn, monkeypatch=None) -> str:
    tid = kb.create_task(conn, title="pair watch", assignee="test-worker")
    kb.claim_task(conn, tid)
    if monkeypatch is not None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))
    return tid


def _row(conn, tid):
    row = conn.execute("SELECT cmd, every, purpose, fails FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()
    return tuple(row) if row else None


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def cli(*args):
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", *args]))


# --- validation ------------------------------------------------------------------------------

def test_validation_defaults_and_bounds():
    req = validate_human_request(_ask("exit 1"))
    assert req["resolved_check"] == "exit 1" and req["resolved_check_every"] == 900
    assert validate_human_request(_ask("exit 1", resolved_check_every=120))["resolved_check_every"] == 120
    assert validate_human_request(_ask("exit 1", resolved_check_every=21600))["resolved_check_every"] == 21600
    assert "resolved_check" not in validate_human_request(ASK)
    for bad in (_ask("exit 1", resolved_check_every=119), _ask("exit 1", resolved_check_every=21601),
                _ask("exit 1", resolved_check_every=True), _ask("exit 1", resolved_check_every="900"),
                _ask("   "), _ask(7), {**ASK, "resolved_check_every": 900}, _ask("x" * 2001)):
        with pytest.raises(ValueError):
            validate_human_request(bad)


# --- filing: synchronous probe once ------------------------------------------------------------

def test_tool_block_files_ask_with_resolved_check(board, monkeypatch):
    from tools import kanban_tools as kt
    flag = _flag(board)
    with kbc.connect_closing() as conn:
        tid = _running(conn, monkeypatch)
        out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                           "human_request": _ask(_probe(flag), resolved_check_every=300)}))
        assert out.get("ok") is True, out
        assert kb.get_task(conn, tid).status == "blocked"
        assert _row(conn, tid) == (_probe(flag), 300, kar.PURPOSE, 0)
        assert kwc.describe_state(kwc.get_state(conn, tid)).startswith("ask resolved_check `")


@pytest.mark.parametrize("cmd,code,needle", [
    ("echo already-paired", kar.ALREADY_RESOLVED, "already-paired"),
    ("echo probe-blew-up; exit 2", kar.BROKEN, "probe-blew-up"),
])
def test_tool_block_refuses_resolved_or_broken_probe(board, monkeypatch, cmd, code, needle):
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        tid = _running(conn, monkeypatch)
        out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                           "human_request": _ask(cmd)}))
        assert out.get("rejection") == code, out
        assert needle in out["error"]
        if code == kar.ALREADY_RESOLVED:
            assert "the asked-for state already holds; don\u2019t ask, continue the work" in out["error"]
        assert kb.get_task(conn, tid).status == "running"
        assert _row(conn, tid) is None


def test_tool_create_blocked_files_or_refuses(board):
    from tools import kanban_tools as kt
    flag = _flag(board)
    base = {"title": "Pair the watch", "initial_status": "blocked", "requires": [], "placement": "local"}
    out = json.loads(kt._handle_create({**base, "human_request": _ask(_probe(flag))}))
    assert out.get("ok") is True, out
    with kbc.connect_closing() as conn:
        tid = out["task_id"]
        assert kb.get_task(conn, tid).status == "blocked"
        assert _row(conn, tid) == (_probe(flag), 900, kar.PURPOSE, 0)
        count = len(kb.list_tasks(conn))
    refused = json.loads(kt._handle_create({**base, "title": "Pair again", "human_request": _ask("true")}))
    assert refused.get("rejection") == kar.ALREADY_RESOLVED, refused
    with kbc.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == count


def test_cli_block_files_or_refuses(board, capsys):
    flag = _flag(board)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        other = _running(conn)
    assert cli("block", other, "need the button", "--kind", "needs_input",
               "--human-request", json.dumps(_ask("echo done-already"))) == 2
    assert kar.ALREADY_RESOLVED in capsys.readouterr().err
    assert cli("block", tid, "need the button", "--kind", "needs_input",
               "--human-request", json.dumps(_ask(_probe(flag)))) == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert _row(conn, tid) == (_probe(flag), 900, kar.PURPOSE, 0)
        assert kb.get_task(conn, other).status == "running" and _row(conn, other) is None


def test_cli_create_blocked_files_or_refuses(board, capsys):
    flag = _flag(board)
    assert cli("create", "Pair", "--initial-status", "blocked", "--human-request", json.dumps(_ask("true"))) == 2
    assert f"{kar.ALREADY_RESOLVED}: the asked-for state already holds" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert not kb.list_tasks(conn)
    assert cli("create", "Pair", "--initial-status", "blocked", "--human-request",
               json.dumps(_ask(_probe(flag))), "--json") == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    with kbc.connect_closing() as conn:
        assert _row(conn, tid) == (_probe(flag), 900, kar.PURPOSE, 0)


def test_cli_metadata_only_adds_changes_and_removes_check(board, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy", assignee="owner", initial_status="blocked", lint_blocked=False)
    a, b = _probe(board / "a"), _probe(board / "b")
    assert cli("block", tid, "Reviewed", "--metadata-only", "--human-request", json.dumps(_ask(a))) == 0
    with kbc.connect_closing() as conn:
        assert _row(conn, tid) == (a, 900, kar.PURPOSE, 0)
    assert cli("block", tid, "Reviewed", "--metadata-only", "--human-request",
               json.dumps(_ask(b, resolved_check_every=600))) == 0
    with kbc.connect_closing() as conn:
        assert _row(conn, tid) == (b, 600, kar.PURPOSE, 0)
    assert cli("block", tid, "Reviewed", "--metadata-only", "--human-request", json.dumps(_ask("true"))) == 2
    assert kar.ALREADY_RESOLVED in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert _row(conn, tid) == (b, 600, kar.PURPOSE, 0)
    assert cli("block", tid, "Reviewed", "--metadata-only", "--human-request", json.dumps(ASK)) == 0
    with kbc.connect_closing() as conn:
        assert _row(conn, tid) is None
        assert kb.get_task(conn, tid).status == "blocked"


def test_human_block_with_bare_wake_check_is_still_refused(board, monkeypatch):
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        tid = _running(conn, monkeypatch)
        out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                           "human_request": ASK, "wake_check": "exit 1"}))
        assert "error" in out and "resolved_check" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


# --- sweep -------------------------------------------------------------------------------------

def _blocked_ask(conn, cmd, every=120, parents=()):
    tid = kb.create_task(conn, title="pair watch", assignee="test-worker", parents=list(parents),
                         initial_status="blocked", human_request=validate_human_request(
                             _ask(cmd, resolved_check_every=every)))
    return tid


def test_sweep_unblocks_when_ask_satisfied_outside_hermes(board):
    flag = _flag(board)
    with kbc.connect_closing() as conn:
        tid = _blocked_ask(conn, _probe(flag))
        now = int(time.time())
        kwc.run_due_wake_checks(conn, now=now + 200, wait=True)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kwc.get_state(conn, tid)["last_rc"] == 1
        flag.write_text("watch paired\n")
        kwc.run_due_wake_checks(conn, now=now + 400, wait=True)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        ev = _events(conn, tid, kar.EVENT)
        assert len(ev) == 1 and ev[0].payload["cmd"] == _probe(flag) and ev[0].payload["rc"] == 0
        assert "watch paired" in ev[0].payload["output"]
        bodies = [c.body for c in kb.list_comments(conn, tid)]
        assert (f"David's ask was satisfied outside Hermes (probe `{_probe(flag)}` exited 0: `watch paired`). "
                "Verify the state and continue; don't redo the asked action.") in bodies
        assert _events(conn, tid, "unblocked")
        assert _row(conn, tid) is None


def test_sweep_unblock_waits_in_todo_behind_open_parent(board):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="test-worker")
        tid = _blocked_ask(conn, f"test -f {board / 'p'} || exit 1", parents=[parent])
        (board / "p").write_text("x")
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 200, wait=True)
        assert kb.get_task(conn, tid).status == "todo"
        assert _events(conn, tid, kar.EVENT)


def test_sweep_stale_rows_dropped_on_archive_and_manual_unblock(board):
    with kbc.connect_closing() as conn:
        archived = _blocked_ask(conn, "exit 1")
        unblocked = _blocked_ask(conn, "exit 1")
        assert _row(conn, archived) and _row(conn, unblocked)
        assert kb.archive_task(conn, archived)
        assert kb.unblock_task(conn, unblocked)
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 200, wait=True)
        assert _row(conn, archived) is None and _row(conn, unblocked) is None
        assert not _events(conn, unblocked, kar.EVENT)


def test_scheduled_wake_check_still_wakes_next_to_ask_rows(board):
    flag = _flag(board)
    flag.write_text("ok")
    with kbc.connect_closing() as conn:
        ask = _blocked_ask(conn, "exit 1")
        parked = kb.create_task(conn, title="t", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parked,))
        kb.claim_task(conn, parked, claimer="worker")
        assert kb.schedule_task(conn, parked, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check=_probe(flag), check_every=120)
        assert _row(conn, parked)[2] == "wake"
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 200, wait=True)
        assert kb.get_task(conn, parked).status == "ready" and _events(conn, parked, "condition_met")
        assert kb.get_task(conn, ask).status == "blocked" and _row(conn, ask)[2] == kar.PURPOSE


def test_broken_resolved_check_comments_once_and_never_unblocks(board):
    script = board / "probe.sh"
    script.write_text("exit 1\n")
    with kbc.connect_closing() as conn:
        tid = _blocked_ask(conn, f"sh {script}")
        script.write_text("echo kaput; exit 2\n")
        now = int(time.time())
        for i in range(kwc.BROKEN_AFTER - 1):
            kwc.run_due_wake_checks(conn, now=now + 200 * (i + 1), wait=True)
            assert kwc.get_state(conn, tid)["fails"] == i + 1
        kwc.run_due_wake_checks(conn, now=now + 200 + kar.BROKEN_MIN_SECONDS, wait=True)
        kwc.run_due_wake_checks(conn, now=now + 400 + kar.BROKEN_MIN_SECONDS, wait=True)
        assert kb.get_task(conn, tid).status == "blocked"
        broken = _events(conn, tid, kar.BROKEN_EVENT)
        assert len(broken) == 1 and "kaput" in broken[0].payload["output"]
        notes = [c.body for c in kb.list_comments(conn, tid) if "resolved_check" in c.body]
        assert len(notes) == 1 and "kaput" in notes[0]
        assert _row(conn, tid) is None and not _events(conn, tid, kar.EVENT)


def test_legacy_wake_checks_table_gains_purpose(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("ALTER TABLE task_wake_checks DROP COLUMN purpose")
            conn.execute("INSERT INTO task_wake_checks (task_id, cmd, every, next_at) VALUES (?, 'exit 1', 900, 0)",
                         (tid,))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)[2] == "wake"


# --- literal exit codes ------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,want", [
    ("echo 'error: looks bad but means not yet'; exit 1", None),
    ("echo 'Traceback (most recent call last)'; exit 0", kar.ALREADY_RESOLVED),
    ("echo 'stale_age=99s gateway=hub'; exit 3", kar.BROKEN),
    ("exit 127", kar.BROKEN),
])
def test_filing_reads_exit_codes_literally(board, cmd, want):
    err = kar.filing_error(_ask(cmd))
    assert (kar.refusal_code(err) if err else None) == want, err


def test_filing_timeout_is_broken(board, monkeypatch):
    monkeypatch.setattr(kwc, "PROBE_TIMEOUT_SECONDS", 1)
    err = kar.filing_error(_ask("sleep 5; exit 1"))
    assert err and err.startswith(kar.BROKEN) and "timeout" in err


def test_sweep_reads_exit_codes_literally(board):
    script = board / "probe.sh"
    with kbc.connect_closing() as conn:
        tid = _blocked_ask(conn, f"sh {script}")
        now = int(time.time())
        script.write_text("echo 'No such file or directory'; exit 1\n")    # still just "not yet"
        kwc.run_due_wake_checks(conn, now=now + 200, wait=True)
        state = kwc.get_state(conn, tid)
        assert state["last_rc"] == 1 and state["fails"] == 0
        script.write_text("echo 'stale_age=120s gateway=hub'; exit 3\n")  # a failure, not "unknown"
        kwc.run_due_wake_checks(conn, now=now + 400, wait=True)
        assert kwc.get_state(conn, tid)["fails"] == 1
        script.write_text("echo 'error: but it is done'; exit 0\n")       # exit 0 is done
        kwc.run_due_wake_checks(conn, now=now + 600, wait=True)
        assert kb.get_task(conn, tid).status == "ready" and _events(conn, tid, kar.EVENT)


def test_sweep_exit_0_runs_the_probe_once(board):
    count = board / "runs"
    with kbc.connect_closing() as conn:
        tid = _blocked_ask(conn, f"echo x >> {count}; test -f {board / 'go'} || exit 1")
        (board / "go").write_text("")
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 200, wait=True)
        assert kb.get_task(conn, tid).status == "ready"
    assert count.read_text().count("x") == 1


# --- filing-vs-stored parity -----------------------------------------------------------------------

def _workspace_task(conn, ws: Path, monkeypatch=None) -> str:
    tid = kb.create_task(conn, title="pair watch", assignee="test-worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind = 'dir', workspace_path = ? WHERE id = ?", (str(ws), tid))
    kb.claim_task(conn, tid)
    if monkeypatch is not None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))
    return tid


def _parity_probe(tid_var: str = "$HERMES_KANBAN_TASK") -> str:
    # exit 1 ("not yet") only when it runs where and as the sweep runs it: the card's workspace, its board pin.
    return f'test -f here-marker && [ -n "$HERMES_KANBAN_DB" ] && [ -n "{tid_var}" ] && exit 1; exit 2'


def test_tool_block_files_in_the_sweeps_cwd_and_env(board, monkeypatch):
    from tools import kanban_tools as kt
    ws = board / "ws"
    ws.mkdir()
    (ws / "here-marker").write_text("")
    with kbc.connect_closing() as conn:
        tid = _workspace_task(conn, ws, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                       "human_request": _ask(_parity_probe())}))
    assert out.get("ok") is True, out
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)[0] == _parity_probe()
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 1000, wait=True)
        state = kwc.get_state(conn, tid)
        assert state["last_rc"] == 1 and state["fails"] == 0                 # the sweep agrees with filing


def test_cli_block_files_in_the_sweeps_cwd_and_env(board, monkeypatch, capsys):
    ws = board / "ws"
    ws.mkdir()
    (ws / "here-marker").write_text("")
    with kbc.connect_closing() as conn:
        tid = _workspace_task(conn, ws)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    assert cli("block", tid, "need the button", "--kind", "needs_input",
               "--human-request", json.dumps(_ask(_parity_probe()))) == 0, capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked" and _row(conn, tid)[0] == _parity_probe()


SECRET_PROBE = "curl -sf -H 'Authorization: Bearer sk-ant-api03-" + "a" * 40 + "' https://hub.local/paired"


def test_secret_probe_refused_on_tool_paths_before_running(board, monkeypatch):
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        tid = _running(conn, monkeypatch)
    out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                       "human_request": _ask(SECRET_PROBE)}))
    assert "error" in out and "resolved_check_secret" in out["error"] and "sk-ant-api03-aaaa" not in out["error"]
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    created = json.loads(kt._handle_create({"title": "Pair", "initial_status": "blocked", "requires": [],
                                            "placement": "local", "human_request": _ask(SECRET_PROBE)}))
    assert "error" in created and "resolved_check_secret" in created["error"], created
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running" and _row(conn, tid) is None
        assert [t.id for t in kb.list_tasks(conn)] == [tid]


def test_secret_probe_refused_on_cli_paths(board, capsys):
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    for args in (("block", tid, "need the button", "--kind", "needs_input"),
                 ("create", "Pair", "--initial-status", "blocked")):
        try:
            rc, msg = cli(*args, "--human-request", json.dumps(_ask(SECRET_PROBE))), ""
        except ValueError as exc:
            rc, msg = "raised", str(exc)
        msg += capsys.readouterr().err
        assert rc != 0 and "resolved_check_secret" in msg and "sk-ant-api03-aaaa" not in msg, (args, rc, msg)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running" and _row(conn, tid) is None
        assert [t.id for t in kb.list_tasks(conn)] == [tid]


def test_padded_probe_stores_exactly_what_filing_ran(board, monkeypatch):
    from tools import kanban_tools as kt
    flag = _flag(board)
    with kbc.connect_closing() as conn:
        tid = _running(conn, monkeypatch)
    out = json.loads(kt._handle_block({"reason": "need the button", "kind": "needs_input",
                                       "human_request": _ask("  " + _probe(flag) + "\n")}))
    assert out.get("ok") is True, out
    with kbc.connect_closing() as conn:
        stored = json.loads(conn.execute("SELECT human_request FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
        assert stored["resolved_check"] == _probe(flag) == _row(conn, tid)[0]


# --- synced board: probe_host on the filing event ----------------------------------------------------

def test_filing_event_names_probe_host_on_a_synced_board(board):
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (gateway_id TEXT)")
            conn.execute("DELETE FROM kanban_sync_state")
            conn.execute("INSERT INTO kanban_sync_state (gateway_id) VALUES ('macbook')")
        tid = _blocked_ask(conn, "exit 1")
        assert _events(conn, tid, "blocked")[-1].payload["probe_host"] == "macbook"
        plain = kb.create_task(conn, title="no probe", assignee="w", initial_status="blocked",
                               human_request=validate_human_request(ASK))
        assert "probe_host" not in _events(conn, plain, "blocked")[-1].payload


# --- every card's own context, before any transition (CLI --ids) ------------------------------------

def test_cli_bulk_block_checks_every_cards_context_before_blocking_any(board, monkeypatch, capsys):
    good, bare = board / "good", board / "bare"
    good.mkdir()
    bare.mkdir()
    (good / "here-marker").write_text("")
    with kbc.connect_closing() as conn:
        first = _workspace_task(conn, good)
        second = _workspace_task(conn, bare)   # its probe would be broken in its own workspace
    probe = "test -f here-marker && exit 1; exit 2"
    assert cli("block", first, "need the button", "--ids", second, "--kind", "needs_input",
               "--human-request", json.dumps(_ask(probe))) == 2
    err = capsys.readouterr().err
    assert kar.BROKEN in err and second in err
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            assert kb.get_task(conn, tid).status == "running" and _row(conn, tid) is None
    (bare / "here-marker").write_text("")
    assert cli("block", first, "need the button", "--ids", second, "--kind", "needs_input",
               "--human-request", json.dumps(_ask(probe))) == 0, capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert all(kb.get_task(conn, t).status == "blocked" and _row(conn, t)[0] == probe for t in (first, second))


# --- create: the sweep's env for a card that does not exist yet ---------------------------------------

def _create_env_probe(db: str) -> str:
    # exit 1 only with the target board pinned and none of the creator's own card/run/claim/session ids.
    return (f'[ "$HERMES_KANBAN_DB" = "{db}" ] && [ -z "$HERMES_KANBAN_TASK" ] && [ -z "$HERMES_KANBAN_RUN_ID" ] '
            '&& [ -z "$HERMES_KANBAN_CLAIM_LOCK" ] && [ -z "$HERMES_SESSION_ID" ] && exit 1; exit 2')


def test_tool_create_files_in_the_sweeps_env_without_the_creators_ids(board, monkeypatch):
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        _running(conn, monkeypatch)                      # a worker creating a card: its own task/run ids are set
        db = str(kbc._main_db_file(conn))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "creator-claim")
    from tests.tools.test_kanban_create_overlap import register_chat
    register_chat(board / '.hermes', 'creator-session')
    monkeypatch.setenv("HERMES_SESSION_ID", "creator-session")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    out = json.loads(kt._handle_create({"title": "Pair the watch", "initial_status": "blocked", "requires": [],
                                        "placement": "local", "human_request": _ask(_create_env_probe(db))}))
    assert out.get("ok") is True, out
    with kbc.connect_closing() as conn:
        assert _row(conn, out["task_id"])[0] == _create_env_probe(db)


def test_cli_create_files_in_the_sweeps_env_without_the_creators_ids(board, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        db = str(kbc._main_db_file(conn))
    from tests.tools.test_kanban_create_overlap import register_chat
    register_chat(board / '.hermes', 's')
    for key, value in (("HERMES_KANBAN_RUN_ID", "7"), ("HERMES_KANBAN_CLAIM_LOCK", "c"),
                       ("HERMES_SESSION_ID", "s")):
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    assert cli("create", "Pair", "--initial-status", "blocked", "--human-request",
               json.dumps(_ask(_create_env_probe(db))), "--json") == 0, capsys.readouterr().err
    tid = json.loads(capsys.readouterr().out)["id"]
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)[0] == _create_env_probe(db)


# --- create: an idempotent retry returns the card and runs no probe -----------------------------------

def _counting_probe(tmp: Path) -> str:
    return f"echo run >> {tmp / 'runs'}; test -f {tmp / 'done'} && exit 0; exit 1"


def test_tool_create_retry_with_idempotency_key_runs_no_probe(board):
    from tools import kanban_tools as kt
    args = {"title": "Pair the watch", "initial_status": "blocked", "requires": [], "placement": "local",
            "idempotency_key": "pair-watch", "human_request": _ask(_counting_probe(board))}
    first = json.loads(kt._handle_create(args))
    assert first.get("ok") is True, first
    (board / "done").write_text("")        # the probe would now refuse a new filing as already resolved
    again = json.loads(kt._handle_create(args))
    assert again.get("ok") is True and again["task_id"] == first["task_id"], again
    assert (board / "runs").read_text().count("run") == 1


def test_cli_create_retry_with_idempotency_key_runs_no_probe(board, capsys):
    argv = ("create", "Pair", "--initial-status", "blocked", "--idempotency-key", "pair-watch",
            "--human-request", json.dumps(_ask(_counting_probe(board))), "--json")
    assert cli(*argv) == 0, capsys.readouterr().err
    tid = json.loads(capsys.readouterr().out)["id"]
    (board / "done").write_text("")
    assert cli(*argv) == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out)["id"] == tid
    assert (board / "runs").read_text().count("run") == 1


# --- dashboard: create also uses the filing guard ----------------------------------------------------

@pytest.mark.parametrize("cmd,code", [("true", kar.ALREADY_RESOLVED), ("exit 2", kar.BROKEN)])
def test_dashboard_create_refuses_resolved_or_broken_probe(board, dashboard, cmd, code):
    r = dashboard.post("/api/plugins/kanban/tasks", json={"title": "Pair watch", "assignee": "test-worker",
        "initial_status": "blocked", "requires": [], "human_request": _ask(cmd)})
    assert r.status_code == 409 and r.json()["detail"].startswith(code), r.text
    with kbc.connect_closing() as conn:
        assert not kb.list_tasks(conn)


def test_dashboard_create_pending_and_idempotent_retry(board, dashboard):
    args = {"title": "Pair watch", "assignee": "test-worker", "initial_status": "blocked", "requires": [],
            "idempotency_key": "rest-pair", "human_request": _ask(_counting_probe(board))}
    first = dashboard.post("/api/plugins/kanban/tasks", json=args)
    assert first.status_code == 200, first.text
    tid = first.json()["task"]["id"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked" and _row(conn, tid)[2] == kar.PURPOSE
    (board / "done").write_text("")
    again = dashboard.post("/api/plugins/kanban/tasks", json=args)
    assert again.status_code == 200 and again.json()["task"]["id"] == tid, again.text
    assert (board / "runs").read_text().count("run") == 1


# --- dashboard: PATCH and bulk run the filing probe too -------------------------------------------------

@pytest.fixture
def dashboard(board, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hermes_cli import web_server
    from plugins.kanban.dashboard import plugin_api as api
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "test-dashboard-token")  # metadata_only needs the gate
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/kanban")
    return TestClient(app, headers={"Authorization": "Bearer test-dashboard-token"})


@pytest.mark.parametrize("cmd,code", [("echo already-paired", kar.ALREADY_RESOLVED), ("exit 2", kar.BROKEN)])
def test_dashboard_patch_block_refuses_before_any_change(board, dashboard, cmd, code):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="pair watch", assignee="test-worker")
    r = dashboard.patch(f"/api/plugins/kanban/tasks/{tid}", json={
        "status": "blocked", "block_kind": "needs_input", "block_reason": "need the button",
        "assignee": "someone-else", "human_request": _ask(cmd)})
    assert r.status_code == 409 and r.json()["detail"].startswith(code), r.text
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.assignee == "test-worker" and _row(conn, tid) is None
    flag = _flag(board)
    r = dashboard.patch(f"/api/plugins/kanban/tasks/{tid}", json={
        "status": "blocked", "block_kind": "needs_input", "block_reason": "need the button",
        "human_request": _ask(_probe(flag))})
    assert r.status_code == 200, r.text
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked" and _row(conn, tid)[0] == _probe(flag)


def test_dashboard_metadata_only_refuses_a_met_probe(board, dashboard):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy", assignee="owner", initial_status="blocked", lint_blocked=False)
    r = dashboard.patch(f"/api/plugins/kanban/tasks/{tid}", json={
        "metadata_only": True, "block_reason": "Reviewed", "human_request": _ask("true")})
    assert r.status_code == 409 and r.json()["detail"].startswith(kar.ALREADY_RESOLVED), r.text
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).human_request is None and _row(conn, tid) is None


def test_dashboard_bulk_checks_each_card_in_its_own_context(board, dashboard):
    good, bare = board / "good", board / "bare"
    good.mkdir()
    bare.mkdir()
    (good / "here-marker").write_text("")
    with kbc.connect_closing() as conn:
        ids = []
        for ws in (good, bare):
            tid = kb.create_task(conn, title="pair watch", assignee="test-worker")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET workspace_kind = 'dir', workspace_path = ? WHERE id = ?",
                             (str(ws), tid))
            ids.append(tid)
    probe = "test -f here-marker && exit 1; exit 2"
    r = dashboard.post("/api/plugins/kanban/tasks/bulk", json={
        "ids": ids, "status": "blocked", "block_kind": "needs_input", "block_reason": "need the button",
        "human_request": _ask(probe)})
    assert r.status_code == 200, r.text
    ok, refused = r.json()["results"]
    assert ok["ok"] is True, ok
    assert refused["ok"] is False and refused["error"].startswith(kar.BROKEN), refused
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, ids[0]).status == "blocked" and _row(conn, ids[0])[0] == probe
        assert kb.get_task(conn, ids[1]).status == "ready" and _row(conn, ids[1]) is None
