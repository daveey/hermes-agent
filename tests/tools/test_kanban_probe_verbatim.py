"""A park's ``wake_check`` is stored exactly as filed, or refused; never silently rewritten. [daveey]

PR #1305's owner parked with a probe ``ssh … "sqlite3 … where idempotency_key='review:pr1305:<sha>'"``. The
kanban tools ran the probe through the display redactor before storing it, which masked the key to
``'review...26b2'``, so the stored probe could never match and the card waited for its fallback wake. A probe
is an executable field: redaction changes what runs. It is now stored byte-identical, and a probe that
redaction would change (a real inline secret) is refused with an explicit error instead of being stored
altered. Comments keep their redaction; non-secret ``*_key`` names (``idempotency_key``) survive it.
"""
from __future__ import annotations

import json

import pytest

KEY = "review:pr1305:848af5adae9e29a2ac677f26c732c996997a26b2"
PROBE = ("ssh ubuntu@hermes-hub \"sqlite3 -readonly ~/.hermes/kanban.db \\\"select 1 from tasks where "
         f"idempotency_key='{KEY}' and status='done'\\\" | grep -q 1\"")
AWAITING = {"kind": "external", "ref": "hub review card", "why": "the hub review card finishes it"}


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")  # the probe ssh's to a host the test has not got
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _stored(tid: str) -> tuple:
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()
        events = [r["payload"] or "" for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
    return (row["cmd"] if row else None), events


def _block(probe: str) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_block({"reason": "waiting on the hub review", "kind": "transient",
                                        "wake_check": probe, "check_every": 600, "awaiting": AWAITING}))


def test_block_stores_idempotency_key_probe_byte_identical(worker_env):
    d = _block(PROBE)
    assert d.get("ok") is True, d
    assert d.get("wake_check") == PROBE  # the echo is the stored command
    cmd, events = _stored(worker_env)
    assert cmd == PROBE
    assert any(KEY in e for e in events)
    assert not any("review..." in e for e in events)


def test_schedule_stores_idempotency_key_probe_byte_identical(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_schedule({"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review",
                                        "wake_check": PROBE, "awaiting": AWAITING}))
    assert d.get("ok") is True, d
    assert _stored(worker_env)[0] == PROBE


@pytest.mark.parametrize("secret_assignment", [
    "OPENAI_API_KEY=sk-proj-" + "Ab3dE" * 8,
    "api_key='sk-" + "Zq9" * 12 + "'",
    "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY",
    "PRIMARY_KEY=" + "Zm9vYmFyYmF6cXV4" * 5 + "K3Rlc3Q=",  # Azure shared-access key: not an identifier name
])
def test_block_with_inline_secret_is_refused_not_masked(worker_env, secret_assignment):
    probe = f"{secret_assignment} curl -sf https://api.example.com/ready"
    d = _block(probe)
    assert "error" in d, d
    assert "wake_check_secret" in json.dumps(d)
    name = secret_assignment.split("=", 1)[0]
    assert name in d["error"]  # names the matched assignment
    assert "env" in d["error"] and "file" in d["error"]
    value = secret_assignment.split("=", 1)[1].strip("'")
    assert value not in json.dumps(d)  # the refusal never echoes the secret
    cmd, events = _stored(worker_env)
    assert cmd is None
    assert not any(value in e for e in events)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, worker_env).status == "running"


def test_refusal_names_a_bare_token_by_its_neighbour():
    from tools.kanban_tools_park_args import _wake_check_secret_error
    token = "ghp_" + "A1b2" * 10
    err = _wake_check_secret_error(f'curl -sf -H "Authorization: Bearer {token}" https://api.github.com/x')
    assert err and err.startswith("wake_check_secret:") and "the value after `Bearer`" in err
    assert token not in err
    first = _wake_check_secret_error(f"{token} | grep -q ok")
    assert first and "its first word" in first and token not in first
    assert _wake_check_secret_error(f"test -f /tmp/x && grep -q '{KEY}' /tmp/x") is None


def test_continue_with_inline_secret_is_refused(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_continue({"checkpoint": "done: x; next: y", "resume_after_seconds": 900,
                                        "wake_check": "GITHUB_TOKEN=ghp_" + "A1b2" * 10 + " gh pr view 1"}))
    assert "wake_check_secret" in json.dumps(d), d
    assert _stored(worker_env)[0] is None


def test_comment_with_idempotency_key_stored_byte_identical(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    body = f"Parked on `{KEY}` with probe: {PROBE}"
    d = json.loads(kt._handle_comment({"task_id": worker_env, "body": body}))
    assert d.get("ok") is True, d
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, worker_env)
    assert comments[-1].body == body


def test_comment_still_masks_real_secret(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    secret = "sk-proj-" + "Ab3dE" * 8
    kt._handle_comment({"task_id": worker_env, "body": f"OPENAI_API_KEY={secret}"})
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, worker_env)
    assert secret not in comments[-1].body
