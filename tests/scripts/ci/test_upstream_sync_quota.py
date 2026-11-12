"""The gate's GitHub request budget: it shares the fleet's user token, so each call stays a handful of reads.

Each gate call used to read ``runs/{id}/jobs`` for every green run in the 50-run list, and the configured
``daveey/hermes`` slug answers every REST call with a 301 to ``softmax-universe/daveey-hermes``, so one
call cost ~100-200 requests and tripped the shared quota brake.
"""
import json
import subprocess

import pytest

from scripts.ci import upstream_sync as sync
from tests.scripts.ci.test_upstream_sync import (
    FakeActions, gate, git, landed_by_merge, main_of, make_world, merge_upstream, upstream_commit,
)

SLUG = "example/fork"


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def run(run_id, head, *, conclusion="success", status="completed", attempt=1):
    return {"id": run_id, "run_number": run_id, "run_attempt": attempt, "head_sha": head,
            "head_branch": sync.BRANCH_PREFIX + head, "event": "push", "path": sync.WORKFLOW_PATH,
            "status": status, "conclusion": conclusion if status == "completed" else None,
            "html_url": f"https://ci.example/run/{run_id}", "updated_at": "2026-11-01T00:00:00Z"}


class CountingAPI:
    """GitHub's REST reads, counted per endpoint; each run's jobs are green unless listed in ``red_runs``."""

    def __init__(self, runs, red_runs=()):
        self.runs, self.red_runs, self.calls = runs, set(red_runs), []

    def request(self, endpoint):
        self.calls.append(endpoint)
        if "/jobs?" in endpoint:
            run_id = int(endpoint.split("/runs/", 1)[1].split("/", 1)[0])
            conclusion = "failure" if run_id in self.red_runs else "success"
            return [{"jobs": [{"name": f"Full suite / {n}", "status": "completed", "conclusion": conclusion,
                               "labels": ["self-hosted"], "runner_name": "hub-1"} for n in sync.REQUIRED_JOBS]}]
        assert f"workflows/{sync.WORKFLOW_FILE}/runs" in endpoint, endpoint
        return [{"workflow_runs": list(reversed(self.runs))}]

    def jobs_reads(self):
        return [c for c in self.calls if "/jobs?" in c]


def fifty_runs(heads):
    """50 completed-green runs over ``heads``, round robin: every head has several reruns."""
    return [run(i + 1, heads[i % len(heads)]) for i in range(50)]


@pytest.mark.parametrize("n_heads", [1, 5, 50])
def test_fifty_runs_read_jobs_once_per_head_at_most(n_heads):
    heads = [f"{i:040x}" for i in range(1, n_heads + 1)]
    api = CountingAPI(fifty_runs(heads))
    found = sync.candidates(SLUG, api.request)
    assert set(found) == set(heads) and {c.status for c in found.values()} == {"green"}
    assert len(api.jobs_reads()) <= n_heads
    newest = {h: max(r["id"] for r in api.runs if r["head_sha"] == h) for h in heads}
    assert {h: c.run_id for h, c in found.items()} == newest  # the newest run is each head's verdict


def test_only_wanted_heads_cost_a_jobs_read():
    heads = [f"{i:040x}" for i in range(1, 11)]
    api = CountingAPI(fifty_runs(heads))
    found = sync.candidates(SLUG, api.request, wanted=lambda head, _updated: head == heads[3])
    assert len(api.jobs_reads()) == 1 and found[heads[3]].status == "green"
    assert {c.status for h, c in found.items() if h != heads[3]} == {"unread"}


def test_newest_verdict_wins_and_a_newer_cancelled_run_does_not_count():
    head = "a" * 40
    api = CountingAPI([run(1, head), run(2, head, conclusion="failure"), run(3, head, conclusion="cancelled")])
    assert sync.candidates(SLUG, api.request)[head].status == "red"  # run 2, not the older green run 1
    assert api.jobs_reads() == []  # a failed run needs no jobs read
    api = CountingAPI([run(1, head, conclusion="failure"), run(2, head)], red_runs={2})
    assert sync.candidates(SLUG, api.request)[head].status == "red"  # newest green run's jobs decide
    assert len(api.jobs_reads()) == 1


def test_completed_run_verdicts_are_cached_by_attempt(tmp_path):
    head = "b" * 40
    path = tmp_path / "cache" / "gate.json"
    api = CountingAPI([run(7, head)])
    assert sync.candidates(SLUG, api.request, cache=sync.VerdictCache(path))[head].status == "green"
    assert len(api.jobs_reads()) == 1
    assert sync.candidates(SLUG, api.request, cache=sync.VerdictCache(path))[head].status == "green"
    assert len(api.jobs_reads()) == 1  # a fresh process reads the stored verdict
    api.runs = [run(7, head, attempt=2)]  # a re-run is a new attempt: its jobs are read again
    api.red_runs = {7}
    assert sync.candidates(SLUG, api.request, cache=sync.VerdictCache(path))[head].status == "red"
    assert len(api.jobs_reads()) == 2
    path.write_text("not json")
    assert sync.candidates(SLUG, api.request, cache=sync.VerdictCache(path))[head].status == "red"  # unreadable: refetch


def test_a_gate_call_reads_jobs_only_for_the_head_it_can_act_on(world):
    """Landed history (many reruns on heads main already holds) costs no jobs reads."""
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    landed = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    before = main_of(root)
    gate(checkout, landed, actions)
    actions.ci()
    assert gate(checkout, landed, actions) == sync.LANDED and landed_by_merge(root, landed, before)
    for _ in range(40):  # GitHub keeps the landed head's (re)runs in the list for weeks
        rerun = dict(actions.runs[0], id=len(actions.runs) + 1, run_number=len(actions.runs) + 1,
                     updated_at="2026-10-01T00:00:00Z")
        actions.runs.append(rerun)

    upstream_commit(root, {"v.txt": "v\n"}, "more upstream")
    head = merge_upstream(checkout)
    calls = []
    request = actions.request

    def counting(endpoint):
        calls.append(endpoint)
        return request(endpoint)
    actions.request = counting
    assert gate(checkout, head, actions) == sync.NOT_YET  # staged
    actions.ci()
    calls.clear()
    actions.aggregate = ("in_progress", None)
    assert gate(checkout, head, actions) == sync.NOT_YET  # landing PR waits for its required check
    jobs = [c for c in calls if "/jobs?" in c]
    head_run = next(r["id"] for r in actions.runs if r["head_sha"] == head)
    assert jobs == [f"repos/{SLUG}/actions/runs/{head_run}/jobs?per_page=100&filter=latest"], calls
    assert len(calls) <= 10, calls


def test_known_renamed_slug_resolves_without_a_request():
    def fail(endpoint):
        raise AssertionError(f"no request expected: {endpoint}")
    assert sync.canonical_slug("daveey/hermes", fail) == "softmax-universe/daveey-hermes"
    assert sync.canonical_slug("Daveey/Hermes", fail) == "softmax-universe/daveey-hermes"


def test_unknown_slug_is_resolved_once_then_cached(tmp_path):
    calls = []

    def request(endpoint):
        calls.append(endpoint)
        return [{"full_name": "new-owner/fork"}]
    path = tmp_path / "gate.json"
    assert sync.canonical_slug(SLUG, request, sync.VerdictCache(path)) == "new-owner/fork"
    assert sync.canonical_slug(SLUG, request, sync.VerdictCache(path)) == "new-owner/fork"
    assert calls == [f"repos/{SLUG}"]

    def broken(endpoint):
        raise sync.GateError("API down")
    assert sync.canonical_slug("other/repo", broken, sync.VerdictCache(None)) == "other/repo"
    assert sync.canonical_slug("other/repo", lambda e: [{"workflow_runs": []}], None) == "other/repo"


def test_main_calls_every_endpoint_on_the_canonical_slug(world, monkeypatch, tmp_path):
    """origin still says daveey/hermes: no REST call may use it (each would cost a 301 plus the redirect)."""
    root, checkout = world
    git(checkout, "config", "remote.origin.url", "git@github.com:daveey/hermes.git")
    git(checkout, "config", f"url.{root / 'origin.git'}.insteadOf", "git@github.com:daveey/hermes.git")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    head = "c" * 40
    api = CountingAPI([run(i, head) for i in range(1, 30)])
    seen = []

    def gh(*args, reason=None):
        seen.append(args)
        if args[:1] == ("api",):
            pages = api.request(args[-1]) if "/pulls?" not in args[-1] else [[]]
            if "--paginate" not in args:  # one named page: GitHub returns that page, not a list of pages
                assert len(pages) == 1
                pages = pages[0]
            return subprocess.CompletedProcess(args, 0, json.dumps(pages), "")
        raise AssertionError(args)
    monkeypatch.setattr(sync, "gh_cli", gh)
    assert sync.main(["--repo", str(checkout)]) == sync.NOT_YET
    endpoints = [a[-1] for a in seen]
    assert endpoints and all(e.startswith("repos/softmax-universe/daveey-hermes/") for e in endpoints), endpoints
    assert len([e for e in endpoints if "/jobs?" in e]) <= 1
    [runs_list] = [a for a in seen if "/workflows/" in a[-1]]
    assert "--paginate" not in runs_list  # the newest 50 runs: one request, not the whole run history


def test_a_named_page_is_read_without_paginating(monkeypatch):
    seen = []

    def gh(*args, reason=None):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"workflow_runs": []} if "--paginate" not in args
                                                               else [{"jobs": []}, {"jobs": []}]), "")
    monkeypatch.setattr(sync, "gh_cli", gh)
    assert sync.github_pages("repos/a/b/actions/workflows/x/runs?per_page=50&page=1") == [{"workflow_runs": []}]
    assert sync.github_pages("repos/a/b/actions/runs/1/jobs?per_page=100") == [{"jobs": []}, {"jobs": []}]
    assert seen == [("api", "repos/a/b/actions/workflows/x/runs?per_page=50&page=1"),
                    ("api", "--paginate", "--slurp", "repos/a/b/actions/runs/1/jobs?per_page=100")]
