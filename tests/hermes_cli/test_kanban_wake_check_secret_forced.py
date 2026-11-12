"""The wake_check secret refusal holds with redaction off, and its text survives the output redactor. [daveey]

``secret_error`` asks ``redact_sensitive_text(cmd, force=True)`` whether a probe carries a secret. With
``force=False`` (mutant M8, verification t_01a1d0154aac633e) the answer follows ``security.redact_secrets`` /
``HERMES_REDACT_SECRETS``, so on a host with redaction off both park paths (tool and CLI) stored and ran an
inline secret, and no test ran with redaction off. Separately, the refusal named the assignment as
``at `OPENAI_API_KEY=`. Probes...``: the CLI's output redactor read ``=`.`` as an assigned value and printed
``at `OPENAI_API_KEY=*** Probes...``, and at a line start the YAML pass read the code ``wake_check_secret: this``
as an assignment and masked ``this``. The name is now given without ``=``, the refusal code is an identifier
name the redactor keeps, and the text is a redaction fixpoint.
"""
from __future__ import annotations

import json

import pytest

from agent import redact
from hermes_cli.kanban_wake_check_secret import secret_error
from tests.hermes_cli import test_kanban_cli_probe_secret as cli
from tests.hermes_cli.test_kanban_cli_probe_secret import SECRET, SECRET_PROBE, UNTIL, _run, _running, _stored

kanban_home = cli.kanban_home  # the CLI suite's isolated board fixture

PROBES = [
    SECRET_PROBE,
    "api_key='sk-" + "Zq9" * 12 + "' curl -sf https://api.example.com/ready",
    "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY" + " aws s3 ls",
    "PRIMARY_KEY=" + "Zm9vYmFyYmF6cXV4" * 5 + "K3Rlc3Q=" + " az storage ls",
    'curl -sf -H "Authorization: Bearer ' + "ghp_" + "A1b2" * 10 + '" https://api.github.com/x',
    "ghp_" + "A1b2" * 10 + " | grep -q ok",
    "GITHUB_TOKEN=ghp_" + "A1b2" * 10 + " OPENAI_API_KEY=" + SECRET + " gh pr view 1",
]


@pytest.fixture
def redaction_off(monkeypatch):
    """Redaction disabled both ways a host can: the env var (read at import) and the module snapshot."""
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    assert redact.redact_sensitive_text(SECRET_PROBE) == SECRET_PROBE  # the switch is really off


@pytest.mark.parametrize("probe", PROBES)
def test_secret_error_refuses_with_redaction_off(redaction_off, probe):
    err = secret_error(probe)
    assert err and err.startswith("wake_check_secret:")


def test_cli_schedule_refuses_with_redaction_off(kanban_home, redaction_off, capsys):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    capsys.readouterr()
    rc = _run("schedule", tid, UNTIL, "--wake-check", SECRET_PROBE)
    out = capsys.readouterr()
    assert rc == 2
    assert "wake_check_secret" in out.err and SECRET not in out.err + out.out
    cmd, events, _comments, status = _stored(tid)
    assert cmd is None and status == "running" and not any(SECRET in e for e in events)


def test_tool_schedule_refuses_with_redaction_off(redaction_off, monkeypatch, tmp_path):
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    d = json.loads(kt._handle_schedule({"reason": f"{UNTIL} waiting on the api", "wake_check": SECRET_PROBE,
                                        "awaiting": {"kind": "external", "ref": "api", "why": "it comes up"}}))
    assert "wake_check_secret" in json.dumps(d), d
    assert SECRET not in json.dumps(d)
    cmd, events, _comments, status = _stored(tid)
    assert cmd is None and status == "running" and not any(SECRET in e for e in events)


@pytest.mark.parametrize("probe", PROBES)
def test_refusal_text_is_stable_under_the_output_redactor(probe):
    err = secret_error(probe)
    assert err
    assert redact.redact_sensitive_text(err, force=True) == err


def test_refusal_names_the_assignment_without_its_value():
    err = secret_error(SECRET_PROBE)
    assert err and "at `OPENAI_API_KEY`." in err
    assert SECRET not in err
