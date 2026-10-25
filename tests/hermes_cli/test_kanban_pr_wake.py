"""PR terminal states wake reconciliation, never certify delivery."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli import kanban_wake_check as wakes

URL = "https://github.com/example/project/pull/44"
pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def probe(tmp_path, monkeypatch):
    # Actual subprocess transport; only GitHub is replaced, never board lifecycle.
    gh = tmp_path / "gh"
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"state": "open", "merged": False, "html_url": URL}))
    gh.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(response)!r}).read_text())\n")
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_platform.resolver import github
    from hermes_platform.resolver import locate_command
    monkeypatch.setattr(github, "resolve_gh", lambda *args: locate_command(str(gh)))
    monkeypatch.setattr(acceptance, "_gh_command_prefix", lambda home: [])
    monkeypatch.setattr(acceptance, "_gh_env", lambda home: None)
    yield response, gh
    wakes._shutdown_for_tests()


def _run():
    from hermes_cli.kanban_pr_wake import inspect_pr
    return inspect_pr(URL)


@pytest.mark.parametrize("state,merged,rc,marker", [
    ("open", False, 1, "pending"),
    ("closed", True, 0, "merged"),
    ("closed", False, 0, "reconcile"),
])
def test_transport_states(probe, state, merged, rc, marker):
    response, _ = probe
    response.write_text(json.dumps({"state": state, "merged": merged, "html_url": URL}))
    actual, message = _run()
    assert actual == rc
    assert marker in message
    if not rc:
        assert "not acceptance" in message


@pytest.mark.parametrize("body", ["oops", "[]", "{}", json.dumps({"state": "closed", "merged": "false", "html_url": URL}), json.dumps({"state": "open", "merged": True, "html_url": URL}), json.dumps({"state": "closed", "merged": False, "html_url": URL + "0"})])
def test_unreadable_or_mismatched_evidence_is_error(probe, body):
    probe[0].write_text(body)
    assert _run()[0] == 2


def test_failed_transport_does_not_log_stderr(probe):
    probe[1].write_text(f"#!{sys.executable}\nimport sys\nprint('SENSITIVE_TOKEN', file=sys.stderr)\nsys.exit(4)\n")
    rc, message = _run()
    assert rc == 2 and "SENSITIVE_TOKEN" not in message


@pytest.mark.parametrize("url", [URL + "/", URL + "?token=SECRET", "http://github.com/example/project/pull/44", "https://evil.invalid/example/project/pull/44", URL.replace("44", "044")])
def test_invalid_identifiers_are_not_normalized(probe, url, monkeypatch):
    from hermes_cli.kanban_pr_wake import inspect_pr
    monkeypatch.setattr(acceptance, "_api", lambda *a, **kw: pytest.fail("invalid URL reached transport"))
    rc, message = inspect_pr(url)
    assert rc == 2 and url not in message


def test_timeout_remains_error_without_detail(probe, monkeypatch):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("SENSITIVE_TOKEN", 30)
    monkeypatch.setattr(acceptance, "_api", timeout)
    rc, message = _run()
    assert rc == 2 and "SENSITIVE_TOKEN" not in message


def test_closed_pr_wakes_real_board_but_does_not_complete(probe, tmp_path, monkeypatch):
    from hermes_cli.kanban_pr_wake import inspect_pr
    probe[0].write_text(json.dumps({"state": "closed", "merged": False, "html_url": URL}))
    assert inspect_pr(URL)[0] == 0
    kb.init_db()
    with kbc.connect_closing() as conn:
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent == tmp_path / "home"
        tid = kb.create_task(conn, title="isolated reconciliation", assignee="fixture-no-dispatch")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="fixture-no-dispatch")
        command = (f"PYTHONPATH={shlex.quote(str(Path(kb.__file__).resolve().parents[1]))} "
                   f"PATH={shlex.quote(str(tmp_path) + ':' + os.environ['PATH'])} "
                   f"{shlex.quote(sys.executable)} -m hermes_cli.kanban_pr_wake {URL}")
        monkeypatch.setattr(wakes, "_profile_home", lambda task: str(tmp_path / "home"))
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", wake_check=command, check_every=120)
        wakes.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        assert kb.get_task(conn, tid).status == "ready", wakes.get_state(conn, tid)["last_out"]
        context = kb.build_worker_context(conn, tid)
        assert "reconcile" in context and "not acceptance" in context
        # Abandoned and superseded PRs are identical until independent evidence exists.
        monkeypatch.setattr(acceptance, "_api", lambda *a, **kw: {"data": {"repository": {"pullRequest": {"state": "CLOSED", "headRefOid": "a" * 40, "baseRefName": "main"}}}})
        receipt = acceptance.collect_acceptance(URL, URL)
        assert not receipt["ok"] and receipt["classification"] == "closed_unmerged"
