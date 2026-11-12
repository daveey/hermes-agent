"""``hermes kanban block/schedule --wake-check`` refuses an inline secret like the tool path does. [daveey]

PR #1354 made the kanban tools refuse a wake_check the display redactor would change (``wake_check_secret``):
a probe runs exactly as stored, so masking it breaks it and keeping it puts the secret on the board. The CLI
park path normalized and dry-ran the probe but had no such refusal, so ``--wake-check 'OPENAI_API_KEY=sk-…'``
was stored and run verbatim, secret included (review t_01a1cf56c1c4cc35). Both paths now share one check, run
before the dry run executes the probe; a non-secret ``idempotency_key='review:…'`` probe is stored byte-identical.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

KEY = "review:pr1305:848af5adae9e29a2ac677f26c732c996997a26b2"
PROBE = ("ssh ubuntu@hermes-hub \"sqlite3 -readonly ~/.hermes/kanban.db \\\"select 1 from tasks where "
         f"idempotency_key='{KEY}' and status='done'\\\" | grep -q 1\"")
SECRET = "sk-proj-" + "Ab3dE" * 8
SECRET_PROBE = f"OPENAI_API_KEY={SECRET} curl -sf https://api.example.com/ready"
UNTIL = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")  # the probe ssh's to a host the test has not got
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


def _run(*argv) -> int:
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    return kcli.kanban_command(parser.parse_args(["kanban", *argv]))


def _stored(tid: str):
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()
        events = [r["payload"] or "" for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
        comments = [c.body for c in kb.list_comments(conn, tid)]
        status = kb.get_task(conn, tid).status
    return (row["cmd"] if row else None), events, comments, status


@pytest.mark.parametrize("verb", [
    ("schedule", UNTIL),
    ("block", UNTIL, "--kind", "time"),
])
def test_cli_park_with_inline_secret_is_refused_and_stores_nothing(kanban_home, capsys, verb):
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    capsys.readouterr()
    rc = _run(verb[0], tid, *verb[1:], "--wake-check", SECRET_PROBE)
    out = capsys.readouterr()
    assert rc == 2
    assert "wake_check_secret" in out.err and "`OPENAI_API_KEY`" in out.err
    assert SECRET not in out.err + out.out  # the refusal never echoes the secret
    cmd, events, comments, status = _stored(tid)
    assert cmd is None and status == "running"
    assert not any(SECRET in e for e in events) and not any(SECRET in c for c in comments)


def test_cli_secret_refusal_runs_before_the_dry_run(monkeypatch):
    from hermes_cli.kanban import _cli_wake_check

    ran = []
    monkeypatch.setattr(kwc, "dry_run_error", lambda cmd, **kw: ran.append(cmd))
    ns = argparse.Namespace(wake_check=SECRET_PROBE, check_every=None, clock_only=False)
    with pytest.raises(ValueError, match="wake_check_secret"):
        _cli_wake_check(ns)
    assert ran == []  # never executed


@pytest.mark.parametrize("verb", [
    ("schedule", UNTIL),
    ("block", UNTIL, "--kind", "time"),
])
def test_cli_park_stores_idempotency_key_probe_byte_identical(kanban_home, verb):
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    assert _run(verb[0], tid, *verb[1:], "--wake-check", PROBE, "--awaiting", "external:hub review card",
                "--awaiting-why", "the hub review card finishes it") == 0
    cmd, events, _comments, _status = _stored(tid)
    assert cmd == PROBE
    assert not any("review..." in e for e in events)
