"""A CI wait on a run that can never start must not sleep until a person notices. [daveey]

Replays the 2026-11-11 hub incident: t_01a1c8d66aa29009 parked (awaiting kind=ci) on GitHub Actions run
42993449998, whose jobs asked for ``self-hosted,ci-fast`` runners that no longer took jobs. The park was accepted
and the card slept until David asked. The fake ``gh`` on PATH serves the run's jobs as GitHub returns them.
"""
from __future__ import annotations

import json
import os
import stat
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

REPO = "softmax-universe/daveey-hermes"
RUN = "42993449998"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN}"
LABELS = ["self-hosted", "ci-fast"]


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ``gh`` executable that answers ``gh api repos/<repo>/actions/runs/<id>/jobs`` from a JSON file."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    jobs = tmp_path / "jobs.json"
    calls = tmp_path / "gh-calls.log"
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAKE_GH_NEEDS_TOKEN') and not os.environ.get('FAKE_BROKER_TOKEN'):\n"
        "    sys.stderr.write('To get started with GitHub CLI, please run:  gh auth login\\n'); sys.exit(4)\n"
        "if os.environ.get('FAKE_GH_401'):\n"
        "    sys.stderr.write('HTTP 401: Bad credentials (https://api.github.com/)\\n'); sys.exit(1)\n"
        "if os.environ.get('FAKE_GH_FAIL'):\n"
        "    sys.stderr.write('HTTP 502: Bad Gateway (https://api.github.com/)\\n'); sys.exit(1)\n"
        f"sys.stdout.write(open({str(jobs)!r}).read())\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    class Gh:
        def serve(self, *, status: str = "queued", age: int, runner: str | None = None) -> None:
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
            started = created if status != "queued" else None
            jobs.write_text(json.dumps({"total_count": 1, "jobs": [{
                "id": 1, "run_id": int(RUN), "name": "state-ci", "status": status, "conclusion": None,
                "created_at": created, "started_at": started, "labels": LABELS, "runner_name": runner,
                "runner_id": 7 if runner else None}]}))

        def calls(self) -> int:
            return len(calls.read_text().splitlines()) if calls.exists() else 0

    return Gh()


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


def _park_on_run(ref: str = RUN_URL, **extra):
    # The probe itself never passes here (a real `gh` would read the real, long-cancelled run off the network).
    return _tool("schedule", {"reason": f"Waiting for state CI run {RUN} to finish.",
                              "wake_check": f"false gh run view {RUN} -R {REPO} --json status",
                              "awaiting": {"kind": "ci", "ref": ref, "why": "state CI on the fix"}, **extra})


def test_park_on_a_run_with_no_runner_for_15_min_is_refused(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=20 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run()
        assert "ci_run_unstartable" in (out.get("error") or ""), out
        assert "ci-fast" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


def test_bare_run_id_uses_the_wake_checks_repo(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=20 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run(ref=RUN)
        assert "ci_run_unstartable" in (out.get("error") or ""), out


@pytest.mark.parametrize("serve", [dict(age=2 * 60), dict(age=40 * 60, status="in_progress", runner="ci-fast-1")])
def test_started_or_freshly_queued_run_parks(kanban_home, fake_gh, monkeypatch, serve) -> None:
    fake_gh.serve(**serve)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run()
        assert out.get("status") == "scheduled", out


def test_github_error_fails_open(kanban_home, fake_gh, monkeypatch, caplog) -> None:
    fake_gh.serve(age=20 * 60)
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        with caplog.at_level("WARNING"):
            out = _park_on_run()
        assert out.get("status") == "scheduled", out
        assert fake_gh.calls() >= 1
        assert any("ci run progress" in r.getMessage() for r in caplog.records), caplog.text


def test_parked_card_wakes_when_its_run_stops_starting(kanban_home, fake_gh, monkeypatch) -> None:
    from hermes_cli import kanban_shared_blockers as ksb

    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        # Its own probe says "not yet" (the run never completes); hours pass with no runner.
        fake_gh.serve(age=20 * 60)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_wake_checks SET cmd = 'exit 1' WHERE task_id = ?", (tid,))
            conn.execute("UPDATE task_wake_checks SET next_at = 0 WHERE task_id = ?", (tid,))
        kwc.run_due_wake_checks(conn, wait=True)
        task = kb.get_task(conn, tid)
        assert task.status == "ready", task.status
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "ci_run_unstartable" in kinds, kinds
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert any(f"run {RUN} queued" in n and "no runner for labels self-hosted,ci-fast" in n for n in notes), notes
        states = ksb.blocker_states(conn)
        assert "runners-offline.ci-fast" in states, states
        assert tid in states["runners-offline.ci-fast"].behind


def test_ci_sweep_reads_github_at_most_every_5_min_per_card(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        parked = fake_gh.calls()
        for _ in range(3):
            kwc.run_due_wake_checks(conn, wait=True)
        assert fake_gh.calls() == parked + 1
        assert kb.get_task(conn, tid).status == "scheduled", [(e.kind, e.payload) for e in kb.list_events(conn, tid)][-3:]

# --- credentials: the hub's gh has no login; its GitHub reads go through the broker prefix (agent-c2b8fc1abf) ---

@pytest.fixture
def no_login(fake_gh, monkeypatch):
    """The hub's ``gh``: exit 4 (auth required) unless the broker put a token in its env."""
    from hermes_cli import kanban_ci_progress as kcp
    monkeypatch.setenv("FAKE_GH_NEEDS_TOKEN", "1")
    monkeypatch.delenv("FAKE_BROKER_TOKEN", raising=False)
    monkeypatch.setattr(kcp, "_WARNED_AT", {}, raising=False)
    return fake_gh


def _configure_prefix(home: Path, prefix: list[str]) -> None:
    # JSON is YAML; the test env has ruamel, not PyYAML.
    (home / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance": {"gh_command_prefix": prefix}}}))


@pytest.fixture
def broker(kanban_home: Path, tmp_path: Path) -> Path:
    """``kanban.pr_acceptance.gh_command_prefix`` = a wrapper that hands gh a token, like ``token-broker exec``."""
    wrapper = tmp_path / "broker" / "token-broker-exec"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n[ \"$1\" = -- ] && shift\nFAKE_BROKER_TOKEN=granted exec \"$@\"\n")
    wrapper.chmod(0o755)
    _configure_prefix(kanban_home, [str(wrapper), "--"])
    return wrapper


def test_run_read_goes_through_the_configured_broker_prefix(kanban_home, no_login, broker) -> None:
    from hermes_cli.kanban_ci_progress import ci_run_progress
    no_login.serve(age=20 * 60)
    p = ci_run_progress(RUN_URL)
    assert p.state == "not_starting", p
    assert p.labels == LABELS


def _sweep_warnings(caplog, conn) -> list[str]:
    from hermes_cli import kanban_ci_progress as kcp
    kcp._LAST_CHECK.clear()
    caplog.clear()
    with caplog.at_level("WARNING", logger=kcp.logger.name):
        kcp.sweep(conn)
    return [r.getMessage() for r in caplog.records if "progress check disabled" in r.getMessage()]


def test_no_credential_warns_once_an_hour(kanban_home, no_login, monkeypatch, caplog) -> None:
    from hermes_cli import kanban_ci_progress as kcp
    no_login.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"  # fails open
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        first = _sweep_warnings(caplog, conn)
        assert len(first) == 1, caplog.text
        assert "gh api exit 4" in first[0] and "no credential: CI-run progress check disabled" in first[0], first
        assert _sweep_warnings(caplog, conn) == [], caplog.text
        stale = time.monotonic() - kcp.NO_CREDENTIAL_WARN_SECONDS - 1
        monkeypatch.setattr(kcp, "_WARNED_AT", {k: stale for k in kcp._WARNED_AT})
        assert len(_sweep_warnings(caplog, conn)) == 1, caplog.text


def test_unreadable_broker_config_fails_open_without_its_text(kanban_home, no_login) -> None:
    from hermes_cli.kanban_ci_progress import ci_run_progress
    no_login.serve(age=20 * 60)
    _configure_prefix(kanban_home, ["relative-broker", "--token=s3cr3t-value"])
    p = ci_run_progress(RUN_URL)
    assert p.state == "unknown" and p.no_credential, p
    assert "s3cr3t" not in p.detail and "relative-broker" not in p.detail, p.detail


def test_other_github_errors_are_not_credential_errors(kanban_home, fake_gh, monkeypatch) -> None:
    from hermes_cli.kanban_ci_progress import ci_run_progress
    fake_gh.serve(age=20 * 60)
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    p = ci_run_progress(RUN_URL)
    assert p.state == "unknown" and not p.no_credential, p


def test_refused_token_is_a_credential_error(kanban_home, fake_gh, monkeypatch) -> None:
    from hermes_cli.kanban_ci_progress import ci_run_progress
    fake_gh.serve(age=20 * 60)
    monkeypatch.setenv("FAKE_GH_401", "1")
    p = ci_run_progress(RUN_URL)
    assert p.state == "unknown" and p.no_credential and "HTTP 401" in p.detail, p


def _no_gh(monkeypatch) -> None:
    import types
    from hermes_platform.resolver import github as ghres
    monkeypatch.setattr(ghres, "resolve_gh", lambda path=None: types.SimpleNamespace(found=False, command=[]))


def test_missing_gh_warns_gh_not_installed_not_a_credential_fix(kanban_home, no_login, monkeypatch, caplog) -> None:
    """PR #2039 review: a missing gh was a ``NoCredential`` whose WARNING said to set gh_command_prefix or log gh
    in. Neither helps when there is no gh binary; the operator must install it."""
    from hermes_cli.kanban_ci_progress import ci_run_progress
    no_login.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        _no_gh(monkeypatch)
        p = ci_run_progress(RUN_URL)
        assert p.state == "unknown" and p.gh_missing and not p.no_credential, p
        warned = _sweep_warnings(caplog, conn)
    assert len(warned) == 1, caplog.text
    assert "gh not installed" in warned[0], warned
    assert "gh_command_prefix" not in warned[0] and "log gh in" not in warned[0], warned


# --- a multi-home process: each board's sweep uses its own home (PR #2039 review) ---------------------------

def _other_home(tmp_path: Path) -> Path:
    other = tmp_path / "other-home"
    other.mkdir()
    return other


def _park_stuck_card_in(home: Path, monkeypatch, gh) -> tuple[str, str]:
    """Under ``home``'s override: a board in that home with a card parked on RUN, whose run then stops starting.
    Returns ``(task id, board db path)``."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        kb.init_db()
        with kbc.connect_closing() as conn:
            tid = _running(conn)
            _as_worker(monkeypatch, conn, tid)
            gh.serve(age=2 * 60)
            assert _park_on_run().get("status") == "scheduled"
            monkeypatch.delenv("HERMES_KANBAN_TASK")
            db = str(kbc._main_db_file(conn))
    finally:
        reset_hermes_home_override(token)
    gh.serve(age=20 * 60)
    return tid, db


def test_sweep_on_a_pool_thread_uses_the_dispatching_homes_prefix(kanban_home, tmp_path, no_login,
                                                                    monkeypatch) -> None:
    """The launch home has no gh_command_prefix; the dispatching home (bound in the dispatcher thread) has the
    broker. ``submit_sweep`` runs ``_sweep_db`` on a pool thread, where the context-local home override is not
    inherited, so the read used the launch home's (empty) prefix, failed auth and never woke the card."""
    from concurrent.futures import ThreadPoolExecutor

    from hermes_cli import kanban_ci_progress as kcp
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    other = _other_home(tmp_path)
    wrapper = tmp_path / "broker-other"
    wrapper.write_text("#!/bin/sh\n[ \"$1\" = -- ] && shift\nFAKE_BROKER_TOKEN=granted exec \"$@\"\n")
    wrapper.chmod(0o755)
    _configure_prefix(other, [str(wrapper), "--"])
    tid, db = _park_stuck_card_in(other, monkeypatch, no_login)
    kcp._LAST_CHECK.clear()
    token = set_hermes_home_override(other)
    try:
        with kbc.connect_closing(Path(db)) as conn, ThreadPoolExecutor(1) as pool:
            fut = kcp.submit_sweep(conn, pool)
            assert fut is not None
            assert fut.result(timeout=60) == [tid]
    finally:
        reset_hermes_home_override(token)


def test_no_credential_warning_is_per_home(kanban_home, tmp_path, no_login, monkeypatch, caplog) -> None:
    """One home's hourly no-credential WARNING must not hide another home's in the same process. Both homes
    read the same board db (the default board is the root ``kanban.db``), so only the home half of the
    ``_WARNED_AT`` key tells them apart: a key on the board db alone warns once (PR #2068 review)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    other = _other_home(tmp_path)
    warned, dbs = [], []
    for home in (kanban_home, other):
        _tid, db = _park_stuck_card_in(home, monkeypatch, no_login)
        dbs.append(db)
        token = set_hermes_home_override(home)
        try:
            with kbc.connect_closing(Path(db)) as conn:
                warned.append(_sweep_warnings(caplog, conn))
        finally:
            reset_hermes_home_override(token)
    assert dbs[0] == dbs[1], dbs
    assert [len(w) for w in warned] == [1, 1], warned


def test_refused_scope_bind_does_not_escape_the_wake_check_tick(kanban_home, fake_gh, monkeypatch, caplog) -> None:
    """PR #2068 review: ``_sweep_db`` binds the dispatching home before ``sweep``'s never-raises guard. A refused
    bind (``ScopeSwitchRefused``) raised out of the pool future, and ``run_due_wake_checks(wait=True)`` only
    catches timeouts there, so it escaped the tick."""
    from contextlib import contextmanager

    from agent import runtime_scope
    from hermes_cli import kanban_ci_progress as kcp
    from hermes_scope import ScopeSwitchRefused

    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        fake_gh.serve(age=20 * 60)
        kcp._LAST_CHECK.clear()

        @contextmanager
        def refused(home, **_kw):
            raise ScopeSwitchRefused("refused inside a hosted scope")
            yield  # pragma: no cover

        monkeypatch.setattr(runtime_scope, "bind_profile_scope", refused)
        with caplog.at_level("WARNING", logger=kcp.logger.name):
            out = kwc.run_due_wake_checks(conn, wait=True)
        assert isinstance(out, dict), out
        assert kb.get_task(conn, tid).status == "scheduled"
        assert any("ci run progress" in r.getMessage() and r.exc_info for r in caplog.records), caplog.text
