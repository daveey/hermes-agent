"""A CI wait on a run that can never start must not sleep until a person notices. [daveey]

Replays the 2026-11-11 hub incident: t_01a1c8d66aa29009 parked (awaiting kind=ci) on GitHub Actions run
42993449998, whose jobs asked for ``self-hosted,ci-fast`` runners that no longer took jobs. The park was accepted
and the card slept until David asked. The fake ``gh`` on PATH serves the run's jobs as GitHub returns them, and the
repo/org runner lists when a test provides them (403 otherwise, as for a token without admin scope).
"""
from __future__ import annotations

import json
import os
import stat
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import Future
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc

REPO = "softmax-universe/daveey-hermes"
RUN = "42993449998"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN}"
LABELS = ["self-hosted", "ci-fast"]
BLOCKER = "runners-offline.ci-fast"


def _iso(age: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))


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
    for state in (kcp._LAST_CHECK, kcp._LAST_SWEEP, kcp._SWEEPS, kcp._POOL_SCANS):
        state.clear()


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ``gh`` executable that answers ``gh api repos/<repo>/actions/runs/<id>/jobs`` from a JSON file, and the
    repo/org ``actions/runners`` lists from their files when present (HTTP 403 when absent)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    jobs = tmp_path / "jobs.json"
    repo_runners = tmp_path / "runners-repo.json"
    org_runners = tmp_path / "runners-org.json"
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
        "path = sys.argv[-1]\n"
        "if '/actions/runners' in path:\n"
        f"    src = {str(org_runners)!r} if path.startswith('orgs/') else {str(repo_runners)!r}\n"
        "    if not os.path.exists(src):\n"
        "        sys.stderr.write('HTTP 403: Resource not accessible by integration\\n'); sys.exit(1)\n"
        "    sys.stdout.write(open(src).read()); sys.exit(0)\n"
        "if '/actions/runs?' in path:\n"
        "    status = path.split('status=', 1)[-1].split('&', 1)[0]\n"
        f"    src = os.path.join({str(tmp_path)!r}, f'runs-{{status}}.json')\n"
        "    sys.stdout.write(open(src).read() if os.path.exists(src) else '{\"workflow_runs\": []}'); sys.exit(0)\n"
        f"other = os.path.join({str(tmp_path)!r}, 'jobs-' + path.split('/runs/', 1)[-1].split('/', 1)[0] + '.json')\n"
        "if os.path.exists(other):\n"
        "    sys.stdout.write(open(other).read()); sys.exit(0)\n"
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

        def runners(self, *runners: dict, org: bool = False) -> None:
            (org_runners if org else repo_runners).write_text(json.dumps({"total_count": len(runners),
                                                                          "runners": list(runners)}))

        def recent(self, status: str, run_id: int, *, updated_age: int, jobs: list[dict]) -> None:
            """One recent run of the repo with ``status`` (as the run list shows it) and its jobs."""
            self.runs(status, (run_id, updated_age, jobs))

        def runs(self, status: str, *runs: tuple[int, int, list[dict] | None]) -> None:
            """The repo's run list for ``status``, newest first: ``(run id, updated age, jobs)`` each."""
            (tmp_path / f"runs-{status}.json").write_text(json.dumps({"total_count": len(runs), "workflow_runs": [
                {"id": run_id, "status": status, "updated_at": _iso(age)} for run_id, age, _jobs in runs]}))
            for run_id, _age, run_jobs in runs:
                if run_jobs is None:  # served from jobs.json (the queued run itself)
                    continue
                (tmp_path / f"jobs-{run_id}.json").write_text(json.dumps({"total_count": len(run_jobs),
                                                                          "jobs": run_jobs}))

        def calls(self, what: str = "") -> int:
            lines = calls.read_text().splitlines() if calls.exists() else []
            return sum(1 for line in lines if what in line)

    return Gh()


def _runner(status: str = "online", labels=("self-hosted", "Linux", "X64", "ci-fast"), busy: bool = True) -> dict:
    return {"id": 3, "name": "ci-fast-3", "os": "Linux", "status": status, "busy": busy,
            "labels": [{"id": i, "name": n, "type": "custom"} for i, n in enumerate(labels)]}


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


def _parked(conn, monkeypatch) -> str:
    """A card parked on the run while it was freshly queued (accepted), as the dispatcher sees it afterwards."""
    tid = _running(conn)
    _as_worker(monkeypatch, conn, tid)
    assert _park_on_run().get("status") == "scheduled"
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    return tid


def _key(conn, tid) -> tuple[str, str]:
    return (str(kbc._main_db_file(conn)), tid)


# --- park time ------------------------------------------------------------------------------------------------

def test_park_on_a_run_with_no_runner_for_15_min_is_refused(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=20 * 60)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run()
        assert "ci_run_unstartable" in (out.get("error") or ""), out
        assert "ci-fast" in out["error"], out
        assert "no runner picked it up in 15 min" in out["error"], out
        assert "runner list unreadable, so they may be offline or saturated" in out["error"], out
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


# --- saturated vs offline runners -----------------------------------------------------------------------------

@pytest.mark.parametrize("org", [False, True], ids=["repo-runners", "org-runners"])
def test_saturated_online_runners_are_not_offline(kanban_home, fake_gh, monkeypatch, org) -> None:
    """Control: every matching runner is online and busy, so the job is only waiting its turn."""
    fake_gh.serve(age=20 * 60)
    junk_labels = _runner()
    junk_labels["labels"].insert(1, "junk")
    fake_gh.runners("junk", junk_labels, org=org)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run()
        assert out.get("status") == "scheduled", out
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert BLOCKER not in ksb.blocker_states(conn)
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY and "ci-fast-3" in p.detail, p


@pytest.mark.parametrize("runner", [_runner(status="offline"), _runner(labels=("self-hosted", "Linux", "ci-slow"))],
                         ids=["offline", "other-labels"])
def test_readable_runner_list_without_a_matching_online_runner_refuses(kanban_home, fake_gh, monkeypatch,
                                                                       runner) -> None:
    fake_gh.serve(age=20 * 60)
    fake_gh.runners(runner)
    fake_gh.runners(org=True)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _park_on_run()
        assert "ci_run_unstartable" in (out.get("error") or ""), out
        assert "no online runner has them" in out["error"], out


@pytest.mark.parametrize("unread", ["org", "repo"])
def test_one_unreadable_runner_list_never_claims_no_online_runner(kanban_home, fake_gh, monkeypatch,
                                                                  unread) -> None:
    """PR #2040 reviews 5889677459/5890809735: a repo served only by org runners, read with a token lacking org admin,
    shows a readable but empty repo list. Nothing says its runners are offline, so the wording falls back; the wake
    (and the park refusal) stay as they were."""
    fake_gh.serve(age=2 * 60)
    if unread == "org":
        fake_gh.runners(_runner(status="offline"))       # repo list readable, no online match; org list 403
    else:
        fake_gh.runners(_runner(status="offline"), org=True)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=20 * 60)
        p = kcp.ci_run_progress(RUN_URL)
        assert p.state == kcp.NOT_STARTING and not p.runners_readable, p
        assert fake_gh.calls("orgs/") >= 1
        assert kcp.sweep(conn) == [tid]
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert "runner list unreadable, so they may be offline or saturated" in notes[-1], notes
        assert "no online runner has them" not in notes[-1], notes


def test_idle_online_runner_that_takes_no_job_wakes_the_card(kanban_home, fake_gh, monkeypatch) -> None:
    """PR #2040 review 5891212407: only an online runner that is busy means saturated. One that sits idle while the
    job waits 15 min is stuck, or in a runner group this repo cannot use."""
    fake_gh.serve(age=2 * 60)
    fake_gh.runners(_runner(busy=False))
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert "runner ci-fast-3 is online and idle for them yet took no job" in notes[-1], notes


@pytest.mark.parametrize("runners", [[_runner(busy=False), {**_runner(), "name": "ci-fast-4"}],
                                     [{k: v for k, v in _runner().items() if k != "busy"}]],
                         ids=["one-busy-among-idle", "no-busy-flag"])
def test_any_busy_runner_reads_as_saturated(kanban_home, fake_gh, runners) -> None:
    """Control: a runner between two jobs is briefly idle; a runner list without the flag keeps the old reading."""
    fake_gh.serve(age=20 * 60)
    fake_gh.runners(*runners)
    assert kcp.ci_run_progress(RUN_URL).state == kcp.BUSY


@pytest.mark.parametrize("age,state", [(kcp.BUSY_LIMIT_SECONDS - 60, kcp.BUSY),
                                       (kcp.BUSY_LIMIT_SECONDS + 60, kcp.NOT_STARTING)],
                         ids=["under-cap", "past-cap"])
def test_busy_runners_hold_a_wake_back_only_up_to_the_cap(kanban_home, fake_gh, monkeypatch, age, state) -> None:
    """PR #2040 review 5890809735: a busy org runner in a group this repo cannot use would hold the wake forever."""
    fake_gh.serve(age=2 * 60)
    fake_gh.runners(_runner(), org=True)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=age)
        assert kcp.ci_run_progress(RUN_URL).state == state
        assert kcp.sweep(conn) == ([tid] if state == kcp.NOT_STARTING else [])
        if state == kcp.NOT_STARTING:
            notes = [c.body for c in kb.list_comments(conn, tid)]
            assert (f"runner ci-fast-3 is online and busy for them, but the job waited past the "
                    f"{kcp.BUSY_LIMIT_SECONDS // 60} min busy cap") in notes[-1], notes


# --- while parked ---------------------------------------------------------------------------------------------

def test_parked_card_wakes_when_its_run_stops_starting(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
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
        assert any(f"run {RUN} queued" in n and "no runner picked it up in 15 min (labels self-hosted,ci-fast" in n
                   for n in notes), notes
        states = ksb.blocker_states(conn)
        assert BLOCKER in states, states
        assert tid in states[BLOCKER].behind
        # The card that found the wall is its probe: it runs now, not after shared_blocker_probe_seconds.
        assert ksb.PROBE_RELEASED_EVENT in kinds, kinds
        assert any(f"is the probe for shared blocker {BLOCKER} and can run now" in n for n in notes), notes
        assert kbd.check_respawn_guard(conn, tid) is None


def test_second_card_woken_behind_the_same_blocker_is_held_and_told_why(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        first, second = sorted([_parked(conn, monkeypatch), _parked(conn, monkeypatch)])
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [first, second]
        assert kbd.check_respawn_guard(conn, first) is None
        assert kbd.check_respawn_guard(conn, second) == ksb.GUARD_REASON
        assert ksb.PROBE_RELEASED_EVENT not in [e.kind for e in kb.list_events(conn, second)]
        notes = [c.body for c in kb.list_comments(conn, second)]
        assert any(f"1 other card(s) ({first}) already wait behind shared blocker {BLOCKER}" in n
                   and "at most every 30 min" in n for n in notes), notes


def test_probe_release_is_spent_by_one_run(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert kbd.check_respawn_guard(conn, tid) is None
        # It ran as the probe and crashed (says nothing about the wall): now it waits the interval like any card.
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome="crashed", summary="probe crashed")
        assert kbd.check_respawn_guard(conn, tid) == ksb.GUARD_REASON


@pytest.mark.parametrize("stale", ["other-blocker", "before-assertion"])
def test_probe_release_must_name_this_blocker_after_the_card_asserted_it(kanban_home, stale) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER})
            kb._append_event(conn, tid, ksb.PROBE_RELEASED_EVENT,
                             {"blocker_id": "runners-offline.ci-slow" if stale == "other-blocker" else BLOCKER})
            if stale == "before-assertion":
                conn.execute("UPDATE task_events SET created_at = created_at - 60 WHERE task_id = ? AND kind = ?",
                             (tid, ksb.PROBE_RELEASED_EVENT))
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert ksb.shared_blocker_guard(conn, tid) == ksb.GUARD_REASON


def test_card_woken_again_for_the_same_blocker_is_the_probe_again(kanban_home, fake_gh, monkeypatch) -> None:
    """PR #2040 review: the card ran as the probe, re-parked on a freshly queued re-run, and that run stalled too.
    Its earlier runs (and the wake's synthesized one) are in ``probe_starts``; the new release must still hold."""
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        _as_worker(monkeypatch, conn, tid)
        fake_gh.serve(age=2 * 60)
        assert _park_on_run().get("status") == "scheduled"
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        kcp._LAST_CHECK.clear()
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert tid in ksb.blocker_states(conn)[BLOCKER].probe_starts
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert f"is the probe for shared blocker {BLOCKER} and can run now" in notes[-1], notes
        assert kbd.check_respawn_guard(conn, tid) is None
        with kb.write_txn(conn):  # its next run spends this release too
            kb._synthesize_ended_run(conn, tid, outcome="crashed", summary="probe crashed")
        assert kbd.check_respawn_guard(conn, tid) == ksb.GUARD_REASON


def test_release_without_a_run_id_is_spent_by_any_probe_start(kanban_home) -> None:
    """A release that names no asserting run (``probe_stranded_blockers``, or written before ``after_run``)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER})
            kb._append_event(conn, tid, ksb.PROBE_RELEASED_EVENT, {"blocker_id": BLOCKER})
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert ksb.shared_blocker_guard(conn, tid) is None
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome="crashed", summary="probe crashed")
        assert ksb.shared_blocker_guard(conn, tid) == ksb.GUARD_REASON


def test_release_without_a_run_id_needs_no_re_assertion_after_it(kanban_home) -> None:
    """PR #2040 review 5889677459: before #2040 the guard ignored ``probe_stranded_blockers`` releases, so another
    card re-asserting the blocker after one held the released card for an interval. It still does."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        other = kb.create_task(conn, title="other", assignee="worker")
        now = int(time.time())
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER})
            conn.execute("UPDATE task_runs SET started_at = ?, ended_at = ? WHERE task_id = ?",
                         (now - 300, now - 300, tid))
            kb._append_event(conn, tid, ksb.PROBE_RELEASED_EVENT, {"blocker_id": BLOCKER})
            conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = ?",
                         (now - 200, tid, ksb.PROBE_RELEASED_EVENT))
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert ksb.shared_blocker_guard(conn, tid, now=now) is None   # control: nothing re-asserted it since
        with kb.write_txn(conn):  # another card hits the wall after the release
            kb._synthesize_ended_run(conn, other, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER})
        assert ksb.blocker_states(conn, now=now)[BLOCKER].asserted_at > now - 200
        assert ksb.shared_blocker_guard(conn, tid, now=now) == ksb.GUARD_REASON


def test_scoped_blocker_card_on_another_provider_does_not_hold_the_woken_card(kanban_home, fake_gh,
                                                                              monkeypatch) -> None:
    """``_others_behind`` counts only the cards ``shared_blocker_guard`` counts (PR #2040 review)."""
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        other = kb.create_task(conn, title="other", assignee="worker")
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, other, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER, "blocker_provider": "anthropic"})
        routes = {other: "openai"}
        monkeypatch.setattr(ksb, "task_route", lambda _c, t: (None, routes.get(t), "ready"))
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert f"is the probe for shared blocker {BLOCKER} and can run now" in notes[-1], notes
        assert ksb.PROBE_RELEASED_EVENT in [e.kind for e in kb.list_events(conn, tid)]


def test_scoped_blocker_card_on_the_same_provider_holds_the_woken_card(kanban_home, fake_gh, monkeypatch) -> None:
    """Control for the test above: the other card's route matches the blocker's provider."""
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        other = kb.create_task(conn, title="other", assignee="worker")
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, other, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                     metadata={"blocker_id": BLOCKER, "blocker_provider": "anthropic"})
        monkeypatch.setattr(ksb, "task_route", lambda _c, t: (None, "anthropic", "ready"))
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        notes = [c.body for c in kb.list_comments(conn, tid)]
        assert f"1 other card(s) ({other}) already wait behind shared blocker {BLOCKER}" in notes[-1], notes


def test_ci_sweep_reads_github_at_most_every_5_min_per_card(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        parked = fake_gh.calls()
        for _ in range(3):
            kcp.sweep(conn)
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


def test_a_raising_pool_callable_does_not_escape_the_wake_check_tick(kanban_home, monkeypatch, caplog) -> None:
    """PR #2086 review: ``run_due_wake_checks(wait=True)`` caught only timeouts around ``fut.result``, so it
    relied on every pool callable never raising. Any exception a pool future carries is logged, not raised."""
    from hermes_cli import kanban_ci_progress as kcp

    def boom() -> list[str]:
        raise RuntimeError("pool callable blew up")

    monkeypatch.setattr(kcp, "submit_sweep", lambda conn, pool, **_kw: pool.submit(boom))
    with kbc.connect_closing() as conn, caplog.at_level("WARNING", logger=kwc.logger.name):
        out = kwc.run_due_wake_checks(conn, wait=True)
    assert out == {"submitted": [], "in_flight": 0}, out
    assert any("pool callable blew up" in str(r.exc_info[1]) for r in caplog.records if r.exc_info), caplog.text


def test_close_failure_after_a_sweep_keeps_the_result_and_says_close(kanban_home, monkeypatch, caplog) -> None:
    """PR #2086 review: ``_sweep_db``'s guard also covered ``conn.close()``, so a close failure after a
    successful sweep was logged as "sweep ... failed" and dropped the woken ids."""
    from hermes_cli import kanban_ci_progress as kcp

    class Conn:
        def close(self) -> None:
            raise sqlite3.OperationalError("disk I/O error on close")

    monkeypatch.setattr(kbc, "connect", lambda *_a, **_k: Conn())
    monkeypatch.setattr(kcp, "sweep", lambda conn: ["t_woken"])
    with caplog.at_level("WARNING", logger=kcp.logger.name):
        assert kcp._sweep_db(str(kanban_home / "kanban.db"), str(kanban_home)) == ["t_woken"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("closing" in m and "after the sweep" in m for m in msgs), msgs
    assert not any("failed" in m and "sweep of" in m for m in msgs), msgs


def test_sweep_forgets_cards_that_stopped_waiting_on_ci(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        kcp.sweep(conn)
        assert _key(conn, tid) in kcp._LAST_CHECK
        other_board = ("/elsewhere/kanban.db", tid)
        kcp._LAST_CHECK[other_board] = time.monotonic()
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kcp.sweep(conn)
        assert _key(conn, tid) not in kcp._LAST_CHECK
        assert other_board in kcp._LAST_CHECK  # another board's sweep owns its own entries


def test_card_woken_after_the_blocker_cleared_is_the_new_probe(kanban_home, fake_gh, monkeypatch) -> None:
    """A card still listed behind a CLEARED blocker is not a probe in flight: the next woken card probes at once."""
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        stale, done = kb.create_task(conn, title="stale", assignee="worker"), kb.create_task(conn, title="d",
                                                                                           assignee="worker")
        with kb.write_txn(conn):
            for card in (stale, done):
                kb._synthesize_ended_run(conn, card, outcome=kcp.CI_RUN_UNSTARTABLE, summary="no runner",
                                         metadata={"blocker_id": BLOCKER})
            kb._synthesize_ended_run(conn, done, outcome="completed", summary="ran on the runner")
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (stale,))
        st = ksb.blocker_states(conn)[BLOCKER]
        assert stale in st.behind and not st.is_open
        fake_gh.serve(age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert ksb.PROBE_RELEASED_EVENT in [e.kind for e in kb.list_events(conn, tid)]


def test_sweep_reads_at_most_max_cards_least_recently_checked_first(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    monkeypatch.setattr(kcp, "MAX_CARDS_PER_SWEEP", 1)
    with kbc.connect_closing() as conn:
        first, second = sorted([_parked(conn, monkeypatch), _parked(conn, monkeypatch)])
        jobs = fake_gh.calls("/jobs")
        kcp.sweep(conn)
        assert fake_gh.calls("/jobs") == jobs + 1
        assert _key(conn, first) in kcp._LAST_CHECK and _key(conn, second) not in kcp._LAST_CHECK
        monkeypatch.setattr(kcp, "RECHECK_SECONDS", 0)  # both due again: the never-checked one goes first
        kcp.sweep(conn)
        assert _key(conn, second) in kcp._LAST_CHECK


def test_sweep_starts_no_read_past_its_time_budget(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    monkeypatch.setattr(kcp, "SWEEP_BUDGET_SECONDS", 0)
    with kbc.connect_closing() as conn:
        _parked(conn, monkeypatch)
        jobs = fake_gh.calls("/jobs")
        kcp.sweep(conn)
        assert fake_gh.calls("/jobs") == jobs


class _Clock:
    """``kcp.time`` with a monotonic clock that each fake ``gh api`` call advances by ``step`` seconds."""

    def __init__(self, step: float) -> None:
        self.now, self.step = 1000.0, step

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _slow_gh(monkeypatch, clock: _Clock, repo_runners: list) -> list[str]:
    """Fake ``_gh_api``: a stuck job, ``repo_runners`` for the repo list; records every path read."""
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 60))
    paths: list[str] = []

    def api(path: str) -> dict:
        paths.append(path.split("?", 1)[0])
        clock.now += clock.step
        if path.startswith("orgs/"):
            return {"runners": [_runner()]}
        if "/actions/runners" in path:
            return {"runners": repo_runners}
        return {"jobs": [{"status": "queued", "runner_name": None, "created_at": created, "labels": LABELS}]}

    monkeypatch.setattr(kcp, "time", clock)
    monkeypatch.setattr(kcp, "_gh_api", api)
    return paths


@pytest.mark.parametrize("step,reads", [(120, 1), (60, 2)], ids=["before-repo-list", "before-org-list"])
def test_sweep_starts_no_gh_call_past_its_budget_and_rereads_that_run_first(kanban_home, fake_gh, monkeypatch,
                                                                            step, reads) -> None:
    """PR #2040 review: a stuck run makes up to three 20 s calls; the budget must stop the runner-list ones too."""
    monkeypatch.setattr(kcp, "SWEEP_BUDGET_SECONDS", 100)
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        paths = _slow_gh(monkeypatch, _Clock(step), [_runner(status="offline")])
        assert kcp.sweep(conn) == []
        assert len(paths) == reads, paths
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _key(conn, tid) not in kcp._LAST_CHECK  # first in line next sweep, not 5 min later
        assert kcp.sweep(conn) == [] and len(paths) == 2 * reads, paths  # re-read at once


def test_runner_list_read_needs_budget_left(kanban_home) -> None:
    def runners(_repo):
        raise AssertionError("runner list read after the deadline")

    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: {"jobs": [{
        "status": "queued", "runner_name": None, "labels": LABELS,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 60))}]},
        fetch_runners=runners, deadline=time.monotonic() - 1)
    assert p.state == "unknown" and p.deferred and p.labels == LABELS, p


@pytest.mark.parametrize("repo_runners,org_read", [([_runner()], False), ([_runner(status="offline")], True)],
                         ids=["repo-online-match", "repo-offline-only"])
def test_org_runner_list_is_read_only_without_an_online_repo_match(kanban_home, monkeypatch, repo_runners,
                                                                   org_read) -> None:
    paths = _slow_gh(monkeypatch, _Clock(0), repo_runners)
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY, p
    assert any(x.startswith("orgs/") for x in paths) is org_read, paths


# --- dispatcher tick cost and pool budget ---------------------------------------------------------------------

def _record_scans(monkeypatch) -> list[threading.Thread]:
    seen: list[threading.Thread] = []
    real = kcp._ci_waits

    def scan(conn):
        seen.append(threading.current_thread())
        return real(conn)

    monkeypatch.setattr(kcp, "_ci_waits", scan)
    return seen


def test_dispatcher_tick_scans_off_thread_and_at_most_once_a_minute(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        _parked(conn, monkeypatch)
        seen = _record_scans(monkeypatch)
        kwc.run_due_wake_checks(conn, wait=True)
        kwc.run_due_wake_checks(conn, wait=True)
        assert len(seen) == 1 and seen[0] is not threading.current_thread(), seen
        monkeypatch.setattr(kcp, "SWEEP_EVERY_SECONDS", 0)
        kwc.run_due_wake_checks(conn, wait=True)
        assert len(seen) == 2, seen


def test_no_sweep_when_no_card_is_scheduled(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        seen = _record_scans(monkeypatch)
        kwc.run_due_wake_checks(conn, wait=True)
        assert seen == []


def test_sweep_starts_only_for_a_scheduled_card_whose_latest_park_is_ci(kanban_home, fake_gh, monkeypatch) -> None:
    """A sweep opens its own connection, which briefly takes the board's dispatch tick lock (a concurrent
    ``dispatch_once`` then returns ``skipped_locked``), so a board whose scheduled cards wait on anything but a CI
    run never starts one. [daveey]"""
    monkeypatch.setattr(kcp, "SWEEP_EVERY_SECONDS", 0)
    started: Future = Future()
    started.set_result([])  # done at once, so the next call is not refused as "a sweep is running"

    class Pool:
        def submit(self, *_a):
            return started

    def latest(conn, tid, payload) -> None:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "scheduled", payload)

    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        assert kcp.submit_sweep(conn, Pool()) is started
        latest(conn, tid, {"reason": "SCHEDULED_UNTIL=later", "awaiting": {"kind": "clock"}})
        assert kcp.submit_sweep(conn, Pool()) is None          # re-parked on a clock: its CI park is history
        latest(conn, tid, {"reason": "back on CI", "awaiting": {"kind": "ci", "ref": RUN_URL}})
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"author": "someone"})   # not a park: the CI park stands
        assert kcp.submit_sweep(conn, Pool()) is started
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kcp.submit_sweep(conn, Pool()) is None          # not parked any more
        other = _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (other,))
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', "
                         "'{not json', ?)", (other, int(time.time())))
        assert kcp.submit_sweep(conn, Pool()) is None
        _parked(conn, monkeypatch)                             # scanned after the malformed one
        assert kcp.submit_sweep(conn, Pool()) is started       # one malformed park does not hide the CI wait


def test_no_second_sweep_of_a_board_while_one_runs(kanban_home, fake_gh, monkeypatch) -> None:
    monkeypatch.setattr(kcp, "SWEEP_EVERY_SECONDS", 0)
    pending: Future = Future()

    class Pool:
        submitted = 0

        def submit(self, *_a):
            Pool.submitted += 1
            return pending

    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        _parked(conn, monkeypatch)
        assert kcp.submit_sweep(conn, Pool()) is pending
        assert kcp.submit_sweep(conn, Pool()) is None
        assert Pool.submitted == 1
        pending.set_result([])
        assert kcp.submit_sweep(conn, Pool()) is pending


def test_two_racing_ticks_on_one_board_start_one_sweep(kanban_home, fake_gh, monkeypatch) -> None:
    """PR #2040 review 5891212407: ``submit_sweep`` releases ``_LOCK`` around ``_has_ci_wait``; both ticks pass the
    first check there, so the second locked block must check again."""
    both_in = threading.Barrier(2, timeout=10)
    real = kcp._has_ci_wait

    def has_ci_wait(conn):
        both_in.wait()
        return real(conn)

    class Pool:
        submitted = 0

        def submit(self, *_a):
            Pool.submitted += 1
            return Future()

    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        _parked(conn, monkeypatch)
    monkeypatch.setattr(kcp, "_has_ci_wait", has_ci_wait)
    results: list = []

    def tick() -> None:
        with kbc.connect_closing() as conn:
            results.append(kcp.submit_sweep(conn, Pool()))

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 2 and Pool.submitted == 1, results
    assert sum(r is not None for r in results) == 1, results


def test_no_sweep_without_a_free_pool_slot(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        _parked(conn, monkeypatch)
        seen = _record_scans(monkeypatch)
        assert kcp.submit_sweep(conn, kwc._pool(), slots_free=0) is None
        # Another board's sweep fills the only slot: this board's waits for it.
        monkeypatch.setattr(kwc, "MAX_CONCURRENT", 1)
        monkeypatch.setitem(kcp._SWEEPS, "/elsewhere/kanban.db", Future())
        kwc.run_due_wake_checks(conn, wait=True)
        assert seen == []


def test_a_running_sweep_counts_against_the_probe_budget(kanban_home, fake_gh, monkeypatch) -> None:
    monkeypatch.setattr(kwc, "MAX_CONCURRENT", 1)
    release = threading.Event()
    started = threading.Event()

    def slow_sweep(_db, _home):
        started.set()
        release.wait(30)
        return []

    monkeypatch.setattr(kcp, "_sweep_db", slow_sweep)
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        with kb.write_txn(conn):  # its own probe is not due yet: this tick only starts the sweep
            conn.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (int(time.time()) + 3600, tid))
        kwc.run_due_wake_checks(conn)
        assert started.wait(10)
        try:
            with kb.write_txn(conn):
                conn.execute("UPDATE task_wake_checks SET next_at = 0 WHERE task_id = ?", (tid,))
            assert kwc.run_due_wake_checks(conn)["submitted"] == []
        finally:
            release.set()
        kcp._SWEEPS.clear()
        assert kwc.run_due_wake_checks(conn, wait=True)["submitted"] == [tid]


# --- runner list unreadable: a job running on the same labels shows the pool alive (2026-11-13, run 43423320884) --
# The hermes-state pool is one runner taking ~10 min jobs back to back; a run queued behind two core PRs waits past
# 15 min. A token without admin scope cannot list runners (the fixture's default: HTTP 403), so the pool's own
# recent jobs are the evidence that it is taking work.

def _taken(*, run_id: int, started_age: int, status: str = "completed", labels=LABELS) -> dict:
    return {"id": run_id * 10, "run_id": run_id, "name": "hermes_state (macOS)", "status": status,
            "conclusion": "success" if status == "completed" else None, "created_at": _iso(started_age + 60),
            "started_at": _iso(started_age), "labels": list(labels), "runner_name": "ci-fast-1", "runner_id": 7}


def test_unreadable_runners_with_a_job_running_on_the_labels_neither_wakes_nor_blocks(kanban_home, fake_gh,
                                                                                       monkeypatch, caplog) -> None:
    fake_gh.serve(age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        fake_gh.serve(age=20 * 60)
        fake_gh.recent("in_progress", 555, updated_age=60, jobs=[_taken(run_id=555, started_age=9 * 60,
                                                                         status="in_progress")])
        with caplog.at_level("INFO", logger=kcp.logger.name):
            assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert "ci_run_unstartable" not in [e.kind for e in kb.list_events(conn, tid)]
        assert BLOCKER not in ksb.blocker_states(conn)
    assert any("runner list unreadable, but runner ci-fast-1 took job 5550" in r.getMessage()
               for r in caplog.records), caplog.text
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY and "ci-fast-1" in p.detail, p


def test_unreadable_runners_with_a_recently_started_job_on_the_labels_parks(kanban_home, fake_gh,
                                                                            monkeypatch) -> None:
    fake_gh.serve(age=20 * 60)
    fake_gh.recent("completed", 556, updated_age=60, jobs=[_taken(run_id=556, started_age=11 * 60)])
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert _park_on_run().get("status") == "scheduled"


@pytest.mark.parametrize("job", [
    _taken(run_id=557, started_age=40 * 60),  # the pool's last job started long ago
    _taken(run_id=557, started_age=5 * 60, labels=("self-hosted", "ci-slow")),  # another pool is alive
], ids=["stale", "other-labels"])
def test_unreadable_runners_and_no_recent_job_on_the_labels_is_not_starting(kanban_home, fake_gh, job) -> None:
    fake_gh.serve(age=20 * 60)
    fake_gh.recent("completed", 557, updated_age=60, jobs=[job])
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.NOT_STARTING and not p.runners_readable, p


def test_org_list_unreadable_without_a_repo_match_falls_back_to_recent_jobs(kanban_home, fake_gh) -> None:
    """A readable repo list with no online match plus a 403 org list counts as unreadable, so the recent-jobs
    evidence applies there too (not only when both lists 403)."""
    fake_gh.serve(age=20 * 60)
    fake_gh.runners(_runner(status="offline"))
    fake_gh.recent("completed", 558, updated_age=60, jobs=[_taken(run_id=558, started_age=5 * 60)])
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY and "ci-fast-1 took job 5580" in p.detail, p


def test_github_hosted_labels_read_no_recent_runs(kanban_home, fake_gh) -> None:
    queued = {"jobs": [{"id": 1, "status": "queued", "created_at": _iso(20 * 60), "labels": ["ubuntu-latest"],
                        "runner_name": None}]}
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda _repo, _run: queued)
    assert p.state == kcp.NOT_STARTING, p
    assert fake_gh.calls("actions/runs?") == 0


def test_unreadable_runners_find_the_pools_running_job_past_the_first_runs(kanban_home, fake_gh) -> None:
    """2026-11-13 ~13:50: 23 in-progress runs, and the one holding the only ``hermes-state`` runner was 21st (of
    another workflow than the queued run). A 4-run window answered ``not_starting`` for a busy pool."""
    fake_gh.serve(age=20 * 60)
    busy_elsewhere = [_taken(run_id=600 + i, started_age=3 * 60, status="in_progress", labels=("self-hosted", "x"))
                      for i in range(20)]
    fake_gh.runs("in_progress", *[(600 + i, 30, [job]) for i, job in enumerate(busy_elsewhere)],
                 (620, 30, [_taken(run_id=620, started_age=3 * 60, status="in_progress")]),
                 *[(621 + i, 30, []) for i in range(4)])
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY and "took job 6200" in p.detail, p
    assert fake_gh.calls("actions/runs?status=in_progress") == 1
    assert fake_gh.calls("/runs/623/jobs") == 1  # read with the match, in its batch of POOL_SCAN_READERS
    assert fake_gh.calls("/runs/624/jobs") == 0  # stops after the batch with the match
    assert fake_gh.calls("status=completed") == 0


def test_pool_scan_reads_use_the_bound_home_on_its_reader_threads(kanban_home, fake_gh, tmp_path,
                                                                   monkeypatch) -> None:
    """PR #2321 review: the parallel job reads ran on fresh pool threads that lost the bound board home, so
    ``dispatcher_gh_argv`` read the launch home's gh_command_prefix. Every read must see the bound home, and the
    scan's answer is kept per home."""
    from agent.runtime_scope import bind_profile_scope
    from hermes_constants import get_hermes_home

    other = tmp_path / "other-home"
    other.mkdir()
    fake_gh.serve(age=20 * 60)
    fake_gh.runs("in_progress", *[(700 + i, 30, []) for i in range(3)])
    seen: list[tuple[str, str]] = []
    real = kcp._gh_api

    def recording(path: str) -> dict:
        seen.append((path.split("?", 1)[0], str(get_hermes_home())))
        return real(path)

    monkeypatch.setattr(kcp, "_gh_api", recording)
    with bind_profile_scope(other, hydrate_secrets=False):
        p = kcp.ci_run_progress(RUN_URL, fetch_runners=lambda *_a, **_k: None)
    assert p.state == kcp.NOT_STARTING, p
    job_reads = [home for path, home in seen if "/runs/70" in path and path.endswith("/jobs")]
    assert len(job_reads) == 3 and set(job_reads) == {str(other)}, seen
    assert {key[0] for key in kcp._POOL_SCANS} == {str(other)}, kcp._POOL_SCANS


def test_unreadable_runners_with_a_long_running_job_on_the_labels_is_busy(kanban_home, fake_gh) -> None:
    """A job the pool's runner started 40 min ago and is still running: the pool is alive, just slow."""
    fake_gh.serve(age=20 * 60)
    fake_gh.recent("in_progress", 558, updated_age=60, jobs=[_taken(run_id=558, started_age=40 * 60,
                                                                     status="in_progress")])
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.BUSY and "took job 5580" in p.detail, p


def test_readable_runner_list_with_only_an_offline_runner_ignores_recent_jobs(kanban_home, fake_gh) -> None:
    """Readable runner lists decide alone: the only runner for the labels is offline, so a job it ran minutes ago (or
    is said to be running) does not make the pool ``busy``."""
    fake_gh.serve(age=20 * 60)
    fake_gh.runners(_runner(status="offline"))
    fake_gh.runners(org=True)  # both lists read: they decide alone
    fake_gh.recent("in_progress", 559, updated_age=60, jobs=[_taken(run_id=559, started_age=3 * 60,
                                                                     status="in_progress")])
    p = kcp.ci_run_progress(RUN_URL)
    assert p.state == kcp.NOT_STARTING and p.runners_readable, p
    assert fake_gh.calls("actions/runs?") == 0


def test_one_repo_scan_answers_every_card_on_the_same_pool_for_a_minute(kanban_home, fake_gh, monkeypatch) -> None:
    fake_gh.serve(age=20 * 60)
    fake_gh.runs("in_progress", (int(RUN), 30, None), (701, 30, []), (702, 30, []))
    first = kcp.ci_run_progress(RUN_URL)
    assert first.state == kcp.NOT_STARTING, first
    assert fake_gh.calls(f"/runs/{RUN}/jobs") == 1  # the queued run itself is not read twice
    assert fake_gh.calls("/runs/701/jobs") == 1 and fake_gh.calls("status=completed") == 1
    assert kcp.ci_run_progress(RUN_URL).state == kcp.NOT_STARTING
    assert fake_gh.calls("actions/runs?") == 2 and fake_gh.calls("/runs/701/jobs") == 1  # remembered
    fake_gh.runs("in_progress", (701, 30, [_taken(run_id=701, started_age=60, status="in_progress")]))
    monkeypatch.setattr(kcp, "POOL_SCAN_TTL_SECONDS", 0)
    assert kcp.ci_run_progress(RUN_URL).state == kcp.BUSY  # expired: scanned again


def test_repo_scan_cut_by_the_budget_is_deferred_and_not_remembered(kanban_home, monkeypatch) -> None:
    clock = _Clock(60)
    queued = {"jobs": [{"id": 1, "status": "queued", "runner_name": None, "created_at": _iso(20 * 60),
                        "labels": LABELS}]}

    def api(path: str) -> dict:
        clock.now += clock.step
        if "actions/runs?" in path:
            return {"workflow_runs": [{"id": 800 + i, "status": "in_progress"} for i in range(5)]}
        return {"jobs": []}

    monkeypatch.setattr(kcp, "time", clock)
    monkeypatch.setattr(kcp, "_gh_api", api)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: queued, fetch_runners=lambda _r: None,
                            deadline=clock.now + 150)
    assert p.state == "unknown" and p.deferred, p
    assert kcp._POOL_SCANS == {}


def test_unreadable_run_list_is_not_remembered(kanban_home, monkeypatch) -> None:
    """A 502 on the run list is no evidence the pool is idle: the next card on the pool reads it again."""
    queued = {"jobs": [{"id": 1, "status": "queued", "runner_name": None, "created_at": _iso(20 * 60),
                        "labels": LABELS}]}

    def api(path: str) -> dict:
        raise RuntimeError("gh api exit 1: HTTP 502")

    monkeypatch.setattr(kcp, "_gh_api", api)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: queued, fetch_runners=lambda _r: None)
    assert p.state == kcp.NOT_STARTING, p
    assert kcp._POOL_SCANS == {}


def _scan_api(monkeypatch, runs: int, jobs_for) -> None:
    """Fake ``_gh_api`` for a repo scan: ``runs`` in-progress runs (ids 900..), each run's jobs from ``jobs_for``.
    Like GitHub, a run list returns at most ``per_page`` runs (default 30)."""
    def api(path: str) -> dict:
        if "actions/runs?" in path:
            if "status=completed" in path:
                return {"workflow_runs": []}
            query = urllib.parse.parse_qs(path.split("?", 1)[1])
            per_page = int(query.get("per_page", ["30"])[0])
            return {"workflow_runs": [{"id": 900 + i, "status": "in_progress"} for i in range(min(runs, per_page))]}
        return {"jobs": jobs_for(int(path.split("/runs/", 1)[1].split("/", 1)[0]))}

    monkeypatch.setattr(kcp, "_gh_api", api)


_QUEUED = {"jobs": [{"id": 1, "status": "queued", "runner_name": None, "created_at": _iso(20 * 60), "labels": LABELS}]}


def test_job_lists_of_a_scan_are_read_concurrently(kanban_home, monkeypatch) -> None:
    """Each read is a broker vend plus a GitHub call (~3 s on the hub): 23 runs one by one ate most of the 90 s
    sweep budget. The six reads of a batch must all be in flight together to pass the barrier."""
    barrier = threading.Barrier(kcp.POOL_SCAN_READERS, timeout=10)

    def jobs_for(run_id: int) -> list:
        barrier.wait()
        return [_taken(run_id=run_id, started_age=60, status="in_progress")] if run_id == 905 else []

    _scan_api(monkeypatch, kcp.POOL_SCAN_READERS, jobs_for)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == kcp.BUSY and "took job 9050" in p.detail, p


def test_scan_lists_more_than_ten_in_progress_runs(kanban_home, monkeypatch) -> None:
    """On 2026-11-13 the run holding the one ``hermes-state`` runner sat at index 23 of 24 in-progress runs: a
    run list read with ``per_page=10`` never sees it and answers ``not_starting`` for a busy pool. The match sits
    at the last index the scan reads, so a run list read with any page size below ``IN_PROGRESS_RUNS_MAX`` misses
    it."""
    last = 900 + kcp.IN_PROGRESS_RUNS_MAX - 1

    def jobs_for(run_id: int) -> list:
        return [_taken(run_id=run_id, started_age=60, status="in_progress")] if run_id == last else []

    _scan_api(monkeypatch, kcp.IN_PROGRESS_RUNS_MAX, jobs_for)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == kcp.BUSY and f"took job {last * 10}" in p.detail, p


@pytest.mark.parametrize("match,state", [(True, kcp.BUSY), (False, kcp.NOT_STARTING)], ids=["match", "no-match"])
def test_one_unreadable_job_list_in_a_scan(kanban_home, monkeypatch, match, state) -> None:
    """A 502 on one run's jobs does not hide a match in another; with no match the scan is not remembered."""
    def jobs_for(run_id: int) -> list:
        if run_id == 901:
            raise RuntimeError("gh api exit 1: HTTP 502")
        return [_taken(run_id=run_id, started_age=60, status="in_progress")] if match and run_id == 903 else []

    _scan_api(monkeypatch, 4, jobs_for)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == state, p
    assert bool(kcp._POOL_SCANS) is match


@pytest.mark.parametrize("match,state", [(True, kcp.BUSY), (False, kcp.NOT_STARTING)], ids=["match", "no-match"])
def test_unreadable_job_list_in_an_earlier_batch_does_not_end_the_scan(kanban_home, monkeypatch, match,
                                                                        state) -> None:
    """A 502 on one run's jobs in the first batch, with no match there, must not stop the scan before a later
    batch's match is read (that read ``not_starting``, the false runners-offline wake); with no match anywhere the
    scan is still not remembered."""
    reads: list[int] = []

    def jobs_for(run_id: int) -> list:
        reads.append(run_id)
        if run_id == 901:
            raise RuntimeError("gh api exit 1: HTTP 502")
        return [_taken(run_id=run_id, started_age=60, status="in_progress")] if match and run_id == 908 else []

    _scan_api(monkeypatch, 10, jobs_for)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == state, p
    assert 908 in reads
    assert bool(kcp._POOL_SCANS) is match


def test_unreadable_job_list_does_not_skip_the_completed_runs(kanban_home, monkeypatch) -> None:
    """An error reading an in-progress run's jobs still lets a recently completed run's job show the pool alive."""
    def api(path: str) -> dict:
        if "actions/runs?" in path:
            if "status=completed" in path:
                return {"workflow_runs": [{"id": 950, "status": "completed", "updated_at": _iso(60)}]}
            return {"workflow_runs": [{"id": 901, "status": "in_progress"}]}
        if "/runs/901/" in path:
            raise RuntimeError("gh api exit 1: HTTP 502")
        return {"jobs": [_taken(run_id=950, started_age=60, status="completed")]}

    monkeypatch.setattr(kcp, "_gh_api", api)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == kcp.BUSY and "took job 9500" in p.detail, p


@pytest.mark.parametrize("match,state", [(True, kcp.BUSY), (False, kcp.NOT_STARTING)], ids=["match", "no-match"])
def test_unreadable_in_progress_run_list_does_not_skip_the_completed_runs(kanban_home, monkeypatch, match,
                                                                          state) -> None:
    """PR #2380: a 502 on the in-progress run list does not end the scan: the completed list is still read, and
    its recent run's job shows the pool alive. With no match there the answer is not remembered."""
    reads: list[str] = []

    def api(path: str) -> dict:
        reads.append(path)
        if "actions/runs?" in path:
            if "status=completed" in path:
                return {"workflow_runs": [{"id": 950, "status": "completed", "updated_at": _iso(60)}]}
            raise RuntimeError("gh api exit 1: HTTP 502")
        labels = LABELS if match else ("self-hosted", "ci-slow")
        return {"jobs": [_taken(run_id=950, started_age=60, status="completed", labels=labels)]}

    monkeypatch.setattr(kcp, "_gh_api", api)
    p = kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert p.state == state, p
    assert match is ("took job 9500" in p.detail), p
    assert any("/runs/950/jobs" in path for path in reads), reads
    assert bool(kcp._POOL_SCANS) is match  # a match is remembered; "none" with an unread list is not


@pytest.mark.parametrize("exc", [KeyError("jobs"), TypeError("bad payload")], ids=["KeyError", "TypeError"])
def test_unexpected_job_read_error_propagates_out_of_the_scan(kanban_home, monkeypatch, exc) -> None:
    """PR #2380: only I/O failures (OSError, RuntimeError, ValueError, TimeoutExpired) are noted and skipped. A bug
    in a job read is not swallowed as "unreadable": ``ci_run_progress`` catches no such error from the pool scan,
    so it reaches the sweep's own handler instead of reading ``not_starting``."""
    def jobs_for(run_id: int) -> list:
        if run_id == 901:
            raise exc
        return []

    _scan_api(monkeypatch, 4, jobs_for)
    with pytest.raises(type(exc)):
        kcp.ci_run_progress(RUN_URL, fetch=lambda *_: _QUEUED, fetch_runners=lambda _r: None)
    assert kcp._POOL_SCANS == {}
