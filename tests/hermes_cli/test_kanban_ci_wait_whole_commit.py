"""A CI park on one GitHub Actions run waits for the whole commit when that run succeeds early. [daveey]

Measured on the hub fleet board (24 h to 2026-11-25): 172 of 435 "Woken: the job on GitHub Actions finished" wakes
re-parked within 3 min doing nothing, 85 of them on ANOTHER run of the same commit: the card parked on one workflow
run while the commit had several, so each run finishing woke it once.

GitHub is stubbed at ``kanban_ci_progress._gh_api`` (the one read every check goes through): runs, their jobs and
the commit's run list. Cases a-h are the acceptance tests of the decided design.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_wake_check as kwc

REPO = "softmax-universe/daveey-hermes"
SHA = "a" * 40
A, B = 43400000001, 43400000002
WIDENED = "ci_wait_widened"
T0 = time.time()


def url(run: int) -> str:
    return f"https://github.com/{REPO}/actions/runs/{run}"


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
    for state in (kcp._LAST_CHECK, kcp._LAST_SWEEP, kcp._SWEEPS, kcp._POOL_SCANS, kcp._WARNED_AT, kcp._JITTER):
        state.clear()


class Api:
    """GitHub as ``gh api <path>`` answers it: workflow runs of one commit, each with one job."""

    def __init__(self) -> None:
        self.runs: dict[int, dict] = {}
        self.commit_fail = False
        self.total_extra = 0  # runs GitHub counts beyond the page it served
        self.calls: list[str] = []

    def run(self, run_id: int, status: str, conclusion: str | None = None, *, name: str | None = None,
            workflow_id: int | None = None, event: str = "push", sha: str = SHA) -> None:
        self.runs[run_id] = {"id": run_id, "name": name or f"wf-{run_id}", "status": status,
                             "workflow_id": workflow_id if workflow_id is not None else run_id % 1000,
                             "event": event, "conclusion": conclusion, "head_sha": sha,
                             "html_url": url(run_id), "created_at": _iso(120), "updated_at": _iso(60)}

    def _jobs(self, run_id: int) -> list[dict]:
        run = self.runs[run_id]
        job_status = {"completed": "completed", "queued": "queued"}.get(run["status"], "in_progress")
        return [{"id": run_id * 10, "run_id": run_id, "name": "tests", "status": job_status,
                 "conclusion": run["conclusion"], "head_sha": run["head_sha"], "created_at": _iso(60),
                 "labels": ["self-hosted", "ci-fast"], "runner_name": None if job_status == "queued" else "r1"}]

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        if path.startswith(f"repos/{REPO}/actions/runs?head_sha="):
            if self.commit_fail:
                raise RuntimeError("gh api exit 1: HTTP 502: Bad Gateway (https://api.github.com/)")
            sha = path.split("head_sha=", 1)[1].split("&", 1)[0]
            runs = [dict(r) for r in self.runs.values() if r["head_sha"] == sha]
            return {"total_count": len(runs) + self.total_extra, "workflow_runs": runs}
        if path.startswith(f"repos/{REPO}/actions/runs/"):
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0].split("?", 1)[0])
            if "/jobs" in path:
                return {"total_count": 1, "jobs": self._jobs(run_id)}
            return dict(self.runs[run_id])
        if "/actions/runners" in path:
            raise RuntimeError("gh api exit 1: HTTP 403: Resource not accessible by integration")
        if "/actions/runs?status=" in path:
            return {"total_count": 0, "workflow_runs": []}
        raise AssertionError(f"unexpected gh api {path}")

    def commit_reads(self) -> int:
        return sum(1 for p in self.calls if "head_sha=" in p)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Api:
    fake = Api()
    monkeypatch.setattr(kcp, "_gh_api", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = T0

        def set(self, t):
            self.now = t
            monkeypatch.setattr(kcp, "_wall_now", lambda: self.now)
    c = Clock()
    c.set(T0)
    return c


def tick(conn, clock, dt=300):
    clock.set(clock.now + dt)
    kcp._LAST_CHECK.clear()
    return kcp.sweep(conn)


def park(conn, run: int = A, **kw) -> str:
    tid = kb.create_task(conn, title=f"card {run}", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.schedule_task(conn, tid, reason=kw.pop("reason", "waiting on CI"), wake_check="false",
                            check_every=900, awaiting={"kind": "ci", "ref": url(run), "why": "test"}, **kw)
    return tid


def status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]


def _widened_card(conn, api, clock) -> str:
    """Case a: parked on A; A succeeds while B (same commit) runs."""
    api.run(A, "in_progress")
    api.run(B, "in_progress")
    tid = park(conn)
    assert tick(conn, clock, 0) == []
    api.run(A, "completed", "success")
    before = comments(conn, tid)
    assert tick(conn, clock) == []
    assert status(conn, tid) == "scheduled"
    assert comments(conn, tid) == before
    assert not events(conn, tid, "condition_met")
    return tid


# a ---------------------------------------------------------------------------------------------------------
def test_a_early_success_with_another_run_pending_widens_and_does_not_wake(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        widened = events(conn, tid, WIDENED)
        since = widened[0].pop("since")
        assert widened == [{"from_run": A, "head_sha": SHA, "pending_runs": [B]}]
        assert isinstance(since, float) and since <= clock.now  # when A went green: the cap counts from here
        # still parked and watched: the card reads as running on GitHub Actions, and one more sweep adds no event
        got = kew.card_state(conn, tid, now=clock.now + 1)
        assert got["state"] == "external_running" and got["external"]["job"] == "running"
        assert tick(conn, clock) == []
        assert len(events(conn, tid, WIDENED)) == 1 and status(conn, tid) == "scheduled"


# b ---------------------------------------------------------------------------------------------------------
def test_b_last_run_of_the_commit_succeeding_wakes_once_naming_every_run(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        before = comments(conn, tid)
        api.run(B, "completed", "success")
        assert tick(conn, clock) == [tid]
        assert status(conn, tid) == "ready"
        met = events(conn, tid, "condition_met")
        assert len(met) == 1 and met[0]["external_result"]["outcome"] == "succeeded"
        new = comments(conn, tid)[len(before):]
        assert len(new) == 1, new
        assert str(A) in new[0] and str(B) in new[0] and new[0].count("success") >= 2, new[0]
        assert tick(conn, clock) == []  # woken once


# c ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_c_widened_then_another_run_fails_wakes_at_once(kanban_home, api, clock, conclusion):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        api.run(B, "completed", conclusion)
        assert tick(conn, clock) == [tid]
        met = events(conn, tid, "condition_met")[-1]
        assert met["external_result"]["outcome"] in ("failed", "cancelled")
        assert any(str(B) in c and conclusion in c for c in comments(conn, tid))


def test_c_one_failure_wakes_while_a_third_run_is_still_pending(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        api.run(B + 1, "queued")
        api.run(B, "completed", "failure")
        assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "failed"


# d ---------------------------------------------------------------------------------------------------------
def test_d_watched_run_failing_wakes_at_once_while_another_runs(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        api.run(A, "in_progress")
        api.run(B, "in_progress")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "failure")
        assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "failed"
        assert not events(conn, tid, WIDENED)
        assert api.commit_reads() == 0  # a failed run is a result: nothing else is read


# e ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("others", ["none", "done"])
def test_e_success_with_nothing_else_pending_wakes_as_today(kanban_home, api, clock, others):
    with kbc.connect_closing() as conn:
        api.run(A, "in_progress")
        if others == "done":
            api.run(B, "completed", "success")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success")
        assert tick(conn, clock) == [tid]
        met = events(conn, tid, "condition_met")
        assert len(met) == 1 and met[0]["external_result"]["outcome"] == "succeeded"
        assert not events(conn, tid, WIDENED)
        assert any("Woken: the job on GitHub Actions finished: succeeded" in c for c in comments(conn, tid))


# f ---------------------------------------------------------------------------------------------------------
def test_f_commit_runs_unreadable_wakes_as_today_and_logs(kanban_home, api, clock, caplog):
    with kbc.connect_closing() as conn:
        api.run(A, "in_progress")
        api.run(B, "in_progress")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success")
        api.commit_fail = True
        with caplog.at_level(logging.WARNING, logger=kcp.logger.name):
            assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "succeeded"
        assert not events(conn, tid, WIDENED)
        logged = [r for r in caplog.records if "commit" in r.getMessage() and r.exc_info]
        assert logged, [r.getMessage() for r in caplog.records]


def test_f_widened_then_commit_unreadable_wakes_on_the_single_run(kanban_home, api, clock, caplog):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        api.commit_fail = True
        with caplog.at_level(logging.WARNING, logger=kcp.logger.name):
            assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "succeeded"
        assert any(r.exc_info for r in caplog.records if "commit" in r.getMessage())


# g ---------------------------------------------------------------------------------------------------------
def test_g_deadline_still_wakes_a_widened_card(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        from hermes_cli.kanban_continuation import wake_due_continuations
        assert tid in wake_due_continuations(conn, now=int(time.time()) + 10 * 24 * 3600)
        assert status(conn, tid) == "ready"


# h ---------------------------------------------------------------------------------------------------------
def test_h_pr_checks_park_is_unchanged(kanban_home, api, clock, monkeypatch):
    """A PR park reads its head's runs through the PR adapter (``_pr_verdict``) and is never widened."""
    pr = 2495
    pr_url = f"https://github.com/{REPO}/pull/{pr}"
    base = {"number": pr, "state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean",
            "head": {"sha": SHA}, "base": {"ref": "state"}, "updated_at": _iso(600)}
    inner = api

    def fake(path: str) -> dict:
        if path == f"repos/{REPO}/pulls/{pr}":
            return json.loads(json.dumps(base))
        if path == f"repos/{REPO}/branches/state":
            return {"name": "state", "protected": False}
        if path == f"repos/{REPO}/rules/branches/state":
            return []
        return inner(path)
    monkeypatch.setattr(kcp, "_gh_api", fake)
    api.run(A, "completed", "success")
    api.run(B, "in_progress")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="pr card", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.schedule_task(conn, tid, reason=f"PR checks @{SHA}", wake_check="false", check_every=900,
                                awaiting={"kind": "ci", "ref": f"{pr_url} checks @{SHA}", "why": "test"})
        assert tick(conn, clock) == []
        assert status(conn, tid) == "scheduled" and not events(conn, tid, WIDENED)
        api.run(B, "completed", "success")
        api.runs[A]["updated_at"] = api.runs[B]["updated_at"] = _iso(600)
        assert tick(conn, clock) == [tid]
        assert not events(conn, tid, WIDENED)


def test_h_non_github_external_watch_is_unchanged(kanban_home, api):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="deploy card", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.schedule_task(conn, tid, reason="deploy", wake_check="probe.sh", check_every=900,
                                awaiting={"kind": "external", "ref": "deploy 7", "why": "test"})
        with kb.write_txn(conn):
            assert kew.note_probe_result(conn, tid, "probe.sh", 1, "", time.time(), 900) is None
            got = kew.note_probe_result(conn, tid, "probe.sh", 0, "", time.time(), 900)
        assert got is not None and got["adapter"] == "shell"  # exit 0 is a finished probe, as before
        assert not events(conn, tid, WIDENED) and api.calls == []


# review follow-ups -------------------------------------------------------------------------------------------
def test_an_older_failed_run_of_the_same_workflow_on_another_event_is_not_hidden(kanban_home, api, clock):
    """push + pull_request runs of ONE workflow on one sha: the older one failing is a result, not superseded."""
    with kbc.connect_closing() as conn:
        api.run(A - 1, "completed", "failure", workflow_id=7, event="push")
        api.run(A, "in_progress", workflow_id=7, event="pull_request")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success", workflow_id=7, event="pull_request")
        assert tick(conn, clock) == [tid]
        met = events(conn, tid, "condition_met")[-1]
        assert met["external_result"]["outcome"] == "failed"
        assert any(str(A - 1) in c and "failure" in c for c in comments(conn, tid))


def test_pr_verdict_keeps_a_failed_run_of_the_same_workflow_on_another_event(api):
    from hermes_cli.kanban_external_adapters import github
    api.run(A - 1, "completed", "failure", workflow_id=7, event="push")
    api.run(A, "completed", "success", workflow_id=7, event="pull_request")
    for r in api.runs.values():
        r["updated_at"] = _iso(600)

    def fake(path):
        if path.endswith("/branches/state"):
            return {"name": "state", "protected": False}
        if path.endswith("/rules/branches/state"):
            return []
        return api(path)
    assert github._pr_verdict(fake, REPO, SHA, "state", None, None)[0] == "failed"


def test_a_widened_wait_is_capped_and_names_the_runs_still_pending(kanban_home, api, clock):
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        api.run(B, "waiting", name="deploy (needs approval)")
        assert tick(conn, clock, kcp.WIDEN_MAX_SECONDS - 600) == []  # under the cap: still parked
        assert status(conn, tid) == "scheduled"
        before = comments(conn, tid)
        assert tick(conn, clock, 900) == [tid]
        met = events(conn, tid, "condition_met")[-1]["external_result"]
        assert met["outcome"] == "pending" and met["pending_runs"] == [B]
        new = comments(conn, tid)[len(before):]
        assert len(new) == 1 and f"deploy (needs approval) #{B} (waiting)" in new[0], new
        assert tick(conn, clock) == [] and len(events(conn, tid, "condition_met")) == 1  # woken once
        assert comments(conn, tid)[len(before):] == new


def test_a_commit_with_more_runs_than_one_page_is_not_widened(kanban_home, api, clock, caplog):
    with kbc.connect_closing() as conn:
        api.run(A, "in_progress")
        api.run(B, "in_progress")
        api.total_extra = 100
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success")
        with caplog.at_level(logging.WARNING, logger=kcp.logger.name):
            assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "succeeded"
        assert not events(conn, tid, WIDENED)
        assert any(r.exc_info and "commit" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("ref", ["same", "other"])
def test_a_new_park_drops_the_widened_scope(kanban_home, api, clock, ref):
    other_sha = "b" * 40
    with kbc.connect_closing() as conn:
        tid = _widened_card(conn, api, clock)
        old_park = kew.park_context(conn, tid)["event_id"]
        assert kew.widened_scope(conn, tid, old_park) is not None
        run = A if ref == "same" else B + 7
        if ref == "other":
            api.run(run, "in_progress", sha=other_sha)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.schedule_task(conn, tid, reason="again", wake_check="false", check_every=900,
                                awaiting={"kind": "ci", "ref": url(run), "why": "test"})
        new_park = kew.park_context(conn, tid)["event_id"]
        assert new_park > old_park and kew.widened_scope(conn, tid, new_park) is None
        if ref == "same":
            # A is green and B still runs: the new park widens anew (its own event), it is not woken
            assert tick(conn, clock) == [] and len(events(conn, tid, WIDENED)) == 2
        else:
            reads = api.commit_reads()
            assert tick(conn, clock) == [] and api.commit_reads() == reads  # a running run: no commit read
            api.run(run, "completed", "success", sha=other_sha)
            assert tick(conn, clock) == [tid]
            assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "succeeded"
            assert len(events(conn, tid, WIDENED)) == 1


def test_over_budget_before_the_commit_read_defers_without_waking_or_widening(kanban_home, api, clock,
                                                                             monkeypatch):
    from hermes_cli.kanban_external_adapters import github
    real = github.poll_commit

    def spent(*a, **kw):
        raise kcp.OverBudget("sweep budget spent before the commit's runs were read")
    with kbc.connect_closing() as conn:
        api.run(A, "in_progress")
        api.run(B, "in_progress")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success")
        records = len(events(conn, tid, kew.WATCH_EVENT))
        monkeypatch.setattr(github, "poll_commit", spent)
        assert tick(conn, clock) == []
        assert status(conn, tid) == "scheduled" and not events(conn, tid, WIDENED)
        assert not events(conn, tid, "condition_met") and len(events(conn, tid, kew.WATCH_EVENT)) == records
        monkeypatch.setattr(github, "poll_commit", real)
        assert tick(conn, clock) == [] and len(events(conn, tid, WIDENED)) == 1


def _pr_fake(api):
    def fake(path):
        if path.endswith("/branches/state"):
            return {"name": "state", "protected": False}
        if path.endswith("/rules/branches/state"):
            return []
        return api(path)
    return fake


@pytest.mark.parametrize("older", [("completed", "cancelled"), ("completed", "skipped"), ("queued", None),
                                   ("waiting", None)])
def test_pr_verdict_an_older_cancelled_or_pending_sibling_stays_superseded(api, older):
    """Only a FAILED older sibling on another event surfaces; cancelled/skipped/pending ones decide nothing."""
    from hermes_cli.kanban_external_adapters import github
    api.run(A - 1, older[0], older[1], workflow_id=7, event="push")
    api.run(A, "completed", "success", workflow_id=7, event="pull_request")
    for r in api.runs.values():
        r["updated_at"] = _iso(600)
    assert github._pr_verdict(_pr_fake(api), REPO, SHA, "state", None, None)[0] == "succeeded"


def test_pr_verdict_over_one_page_is_not_judged_and_logged(api, caplog):
    from hermes_cli.kanban_external_adapters import github
    api.run(A, "completed", "success")
    for r in api.runs.values():
        r["updated_at"] = _iso(600)
    api.total_extra = 100
    with caplog.at_level(logging.WARNING, logger=github.logger.name):
        job, detail = github._pr_verdict(_pr_fake(api), REPO, SHA, "state", None, None)
    assert job == "unknown" and "not judged on one page" in detail
    assert any("not judged on one page" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("older", [("completed", "cancelled"), ("queued", None)])
def test_commit_wait_an_older_cancelled_or_pending_sibling_stays_superseded(kanban_home, api, clock, older):
    """A cancelled (concurrency-group) or still-queued older run of the watched run's workflow on another event
    neither wakes the card as cancelled nor keeps it waiting."""
    with kbc.connect_closing() as conn:
        api.run(A - 1, older[0], older[1], workflow_id=7, event="push")
        api.run(A, "in_progress", workflow_id=7, event="pull_request")
        tid = park(conn)
        tick(conn, clock, 0)
        api.run(A, "completed", "success", workflow_id=7, event="pull_request")
        assert tick(conn, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "succeeded"
        assert not events(conn, tid, WIDENED)
