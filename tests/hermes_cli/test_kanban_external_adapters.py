"""Watch adapters: GitHub run, GitHub PR checks and the legacy shell probe -> one watch poll. [daveey]

A fake GitHub serves 200 / 5xx / 429 with retry-after / 401 / timeout through the ``fetch=`` injection that
``kanban_ci_progress`` already has, so nothing touches the network.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli.kanban_external_adapters import github, shell

REPO = "softmax-universe/daveey-hermes"
RUN = "42993449998"
REF = f"https://github.com/{REPO}/actions/runs/{RUN}"
NOW = 1_800_000_000.0


def _iso(age: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age))


class FakeGitHub:
    """Scripted jobs/run endpoints. ``fail`` raises the next call(s) with a scripted error."""

    def __init__(self):
        self.jobs: list[dict] = []
        self.run: dict = {"status": "in_progress", "conclusion": None}
        self.fail: list[BaseException] = []
        self.calls: list[str] = []

    def fetch(self, repo, run_id):
        self.calls.append("jobs")
        if self.fail:
            raise self.fail.pop(0)
        return {"jobs": self.jobs}

    def fetch_run(self, repo, run_id):
        self.calls.append("run")
        if self.fail:
            raise self.fail.pop(0)
        return self.run

    def poll(self, **kw):
        return github.poll(REF, now=NOW, fetch=self.fetch, fetch_run=self.fetch_run,
                           fetch_runners=lambda repo: [], **kw)


def _job(status, conclusion=None, runner="r1", age=60, name="tests", labels=("self-hosted", "ci-fast")):
    return {"id": 1, "name": name, "status": status, "conclusion": conclusion,
            "runner_name": runner if status != "queued" else None, "created_at": _iso(age),
            "labels": list(labels), "html_url": f"{REF}/job/1"}


def test_in_progress_run_is_running_ok():
    gh = FakeGitHub()
    gh.jobs = [_job("in_progress"), _job("queued", age=30)]
    got = gh.poll()
    assert (got.poll["job"], got.poll["service"]) == ("running", "ok")
    assert got.poll["url"] == REF
    assert got.service_name == "GitHub Actions" and got.adapter == "github_run"
    assert got.scope == REPO


def test_freshly_queued_run_is_queued():
    gh = FakeGitHub()
    gh.jobs = [_job("queued", age=30)]
    assert gh.poll().poll["job"] == "queued"
    gh.jobs = []
    assert gh.poll().poll["job"] == "queued"  # jobs not created yet


@pytest.mark.parametrize("conclusion,job", [("success", "succeeded"), ("failure", "failed"),
                                            ("timed_out", "failed"), ("cancelled", "cancelled"),
                                            ("skipped", "succeeded")])
def test_completed_run_reports_its_conclusion(conclusion, job):
    gh = FakeGitHub()
    gh.jobs = [_job("completed", conclusion)]
    gh.run = {"status": "completed", "conclusion": conclusion}
    assert gh.poll().poll["job"] == job


def test_completed_jobs_of_a_run_still_running_are_not_terminal():
    gh = FakeGitHub()
    gh.jobs = [_job("completed", "success")]
    gh.run = {"status": "in_progress", "conclusion": None}  # a later job has not been created yet
    assert gh.poll().poll["job"] == "running"


def test_stuck_queued_job_is_degraded_and_reported_not_starting():
    gh = FakeGitHub()
    gh.jobs = [_job("queued", age=40 * 60)]
    got = gh.poll()
    assert got.progress.state == kcp.NOT_STARTING
    assert (got.poll["job"], got.poll["service"]) == ("queued", "degraded")
    assert "no runner" in got.poll["detail"]


@pytest.mark.parametrize("error,health", [
    (RuntimeError("gh api exit 1: HTTP 502: Bad Gateway (https://api.github.com/)"), "down"),
    (RuntimeError("gh api exit 1: HTTP 503: Service Unavailable"), "down"),
    (subprocess.TimeoutExpired("gh", 20), "unreachable"),
    (RuntimeError("gh api exit 1: dial tcp: lookup api.github.com: no such host"), "unreachable"),
    (kcp.NoCredential("gh api exit 1: HTTP 401: Bad credentials"), "auth_failed"),
    (kcp.NoCredential("gh unavailable: no broker"), "auth_failed"),
    (RuntimeError("gh api exit 1: HTTP 429: rate limit exceeded"), "rate_limited"),
    (RuntimeError("gh api exit 1: HTTP 403: You have exceeded a secondary rate limit"), "rate_limited"),
])
def test_github_errors_become_health_records_not_silence(error, health):
    gh = FakeGitHub()
    gh.fail = [error]
    got = gh.poll()
    assert got.poll["service"] == health, got.poll
    assert got.poll["job"] == "unknown"
    assert got.poll["detail"]


def test_gh_missing_is_a_broken_probe():
    gh = FakeGitHub()
    gh.fail = [kcp.GhMissing("gh not installed")]
    assert gh.poll().poll["service"] == "probe_broken"


def test_429_retry_after_is_parsed_and_default_is_set():
    gh = FakeGitHub()
    gh.fail = [RuntimeError("gh api exit 1: HTTP 429: rate limit. retry-after: 90")]
    assert gh.poll().poll["retry_after"] == 90
    gh.fail = [RuntimeError("gh api exit 1: HTTP 429: slow down")]
    assert gh.poll().poll["retry_after"] == github.DEFAULT_RETRY_AFTER


def test_sweep_budget_cut_defers_instead_of_recording():
    gh = FakeGitHub()
    gh.jobs = [_job("queued", age=40 * 60)]
    got = github.poll(REF, now=NOW, fetch=gh.fetch, fetch_run=gh.fetch_run, deadline=time.monotonic() - 1)
    assert got.poll is None and got.progress.deferred


def test_untracked_ref_has_no_adapter():
    assert github.poll("a release", now=NOW) is None
    assert github.adapter_for("a release") is None
    assert github.adapter_for(REF) == "github_run"
    assert github.adapter_for(f"{REPO}/pull/12") == "github_pr_checks"


# --- PR checks -----------------------------------------------------------------------------------------------

HEAD = "a" * 40


def _api(routes: dict):
    def api(path):
        for prefix, value in routes.items():
            if path.startswith(prefix):
                if isinstance(value, BaseException):
                    raise value
                return value
        raise AssertionError(f"unexpected api path {path}")
    return api


def _pr(**kw):
    return {"state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean",
            "head": {"sha": HEAD}, "base": {"ref": "main"}, "updated_at": _iso(10), **kw}


def _pr_poll(monkeypatch, routes, sha=HEAD):
    monkeypatch.setattr(kcp, "_gh_api", _api(routes))
    return github.poll(f"{REPO}/pull/12@{sha[:12]}", now=NOW)


def test_pr_with_running_checks_is_running(monkeypatch):
    runs = {"workflow_runs": [{"id": 5, "status": "in_progress", "conclusion": None}]}
    got = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr(), f"repos/{REPO}/actions/runs?head_sha": runs})
    assert got.adapter == "github_pr_checks"
    assert (got.poll["job"], got.poll["service"]) == ("running", "ok")


@pytest.mark.parametrize("conclusions,job", [(["success", "success"], "succeeded"), (["success", "failure"], "failed"),
                                             (["cancelled"], "cancelled")])
def test_pr_with_finished_checks_reports_them(monkeypatch, conclusions, job):
    runs = {"workflow_runs": [{"id": i, "status": "completed", "conclusion": c} for i, c in enumerate(conclusions)]}
    got = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr(), f"repos/{REPO}/actions/runs?head_sha": runs})
    assert got.poll["job"] == job


def test_merged_or_moved_pr_does_not_wake(monkeypatch):
    merged = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr(state="closed", merged=True)})
    assert merged.poll["job"] == "unknown" and "merged" in merged.poll["detail"]
    closed = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr(state="closed")})
    assert closed.poll["job"] == "unknown" and "closed" in closed.poll["detail"]
    moved = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr()}, sha="b" * 40)
    assert moved.poll["job"] == "unknown" and "moved" in moved.poll["detail"]


def test_conflicting_pr_is_degraded_queued(monkeypatch):
    got = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": _pr(mergeable=False, mergeable_state="dirty")})
    assert got.progress.state == kcp.NOT_STARTING
    assert (got.poll["job"], got.poll["service"]) == ("queued", "degraded")


def test_pr_read_5xx_is_down(monkeypatch):
    got = _pr_poll(monkeypatch, {f"repos/{REPO}/pulls/12": RuntimeError("gh api exit 1: HTTP 502: Bad Gateway")})
    assert got.poll["service"] == "down"


# --- legacy shell probes ---------------------------------------------------------------------------------------

def test_shell_return_codes_map_to_polls():
    assert shell.poll(1, "")["job"] == "running"
    assert shell.poll(1, "")["service"] == "ok"
    done = shell.poll(0, "")
    assert (done["job"], done["terminal"]) == ("unknown", True)
    assert done["outcome"] == "done_unknown"
    unk = shell.poll(3, "stale_age=60s gateway=w1")
    assert (unk["job"], unk["service"]) == ("unknown", "ok")
    for rc in (2, 127, None):
        assert shell.poll(rc, "boom")["service"] == "probe_broken"
    crashed = shell.poll(1, "Traceback (most recent call last):\n  boom")
    assert crashed["service"] == "probe_broken"


def test_hermes_watch_line_overrides_the_return_code_meaning():
    out = 'noise\nHERMES_WATCH {"job": "failed", "service": "ok", "detail": "tests red", "url": "https://x/1"}\n'
    got = shell.poll(0, out)
    assert (got["job"], got["terminal"], got["url"], got["detail"]) == ("failed", True, "https://x/1", "tests red")
    down = shell.poll(1, 'HERMES_WATCH {"job": "unknown", "service": "down", "detail": "502"}')
    assert (down["service"], down["terminal"]) == ("down", False)
    limited = shell.poll(1, 'HERMES_WATCH {"job": "running", "service": "rate_limited", "retry_after": 120}')
    assert (limited["service"], limited["retry_after"]) == ("rate_limited", 120)
    named = shell.poll(1, 'HERMES_WATCH {"job": "running", "service": "ok", "service_name": "Slurm"}')
    assert named["service_name"] == "Slurm"


def test_malformed_or_hostile_watch_lines_are_ignored():
    assert shell.poll(1, "HERMES_WATCH {not json}")["job"] == "running"
    assert shell.poll(1, 'HERMES_WATCH {"job": "exploded", "service": "fine"}')["job"] == "running"
    assert shell.parse_watch("HERMES_WATCH []") is None
    # the last valid line wins
    out = 'HERMES_WATCH {"job": "running"}\nHERMES_WATCH {"job": "succeeded"}'
    assert shell.parse_watch(out)["job"] == "succeeded"
    assert len(shell.poll(1, 'HERMES_WATCH {"job":"running","detail":"' + "x" * 5000 + '"}')["detail"]) <= 300
