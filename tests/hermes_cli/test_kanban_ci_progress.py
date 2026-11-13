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