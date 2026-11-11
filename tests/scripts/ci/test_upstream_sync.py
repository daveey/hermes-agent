"""Upstream sync gate: real bare Git remotes and real pytest verdicts; only GitHub's API is a fixture.

The fixture API reports for each staged ``sync/upstream-<sha>`` branch the verdict a real pytest run
on that exact checkout produced, so "red is refused, green lands" is decided by real test code.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from scripts.ci import upstream_sync as sync


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def commit(checkout, files, msg):
    for name, text in files.items():
        (checkout / name).write_text(text)
    git(checkout, "add", ".")
    git(checkout, "commit", "-q", "-m", msg)
    return git(checkout, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def make_world(tmp_path):
    """origin (the fork) and upstream bare remotes, and a fork checkout tracking both."""
    for name in ("origin.git", "upstream.git"):
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / name)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        git(seed, "config", k, v)
    commit(seed, {"test_suite.py": "def test_suite(): assert True\n"}, "base")
    git(seed, "push", "-q", str(tmp_path / "origin.git"), "main")
    git(seed, "push", "-q", str(tmp_path / "upstream.git"), "main")
    checkout = tmp_path / "fork"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(checkout)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        git(checkout, "config", k, v)
    git(checkout, "remote", "add", "upstream", str(tmp_path / "upstream.git"))
    return tmp_path, checkout


def upstream_commit(world_root, files, msg):
    up = world_root / "up-work"
    if not up.exists():
        subprocess.run(["git", "clone", "-q", str(world_root / "upstream.git"), str(up)], check=True)
        for k, v in (("user.email", "u@example.com"), ("user.name", "U")):
            git(up, "config", k, v)
    git(up, "pull", "-q")
    sha = commit(up, files, msg)
    git(up, "push", "-q", "origin", "main")
    return sha


def merge_upstream(checkout):
    git(checkout, "fetch", "-q", "upstream")
    git(checkout, "fetch", "-q", "origin")
    git(checkout, "checkout", "-q", "--detach", "origin/main")
    git(checkout, "merge", "-q", "--no-edit", "--no-ff", "upstream/main")
    return git(checkout, "rev-parse", "HEAD")


class FakeActions:
    """GitHub for the bare origin: Actions runs real pytest on each staged sync branch; PRs merge like GitHub.

    A landing PR gets ci.yaml's required check (``aggregate``: None = no run yet, else (status,
    conclusion)) on its head. ``gh pr merge`` refuses unless that check is green on the exact head
    (enforce_admins: no --admin bypass), then writes GitHub's merge commit (main, head) server-side.
    """

    def __init__(self, origin: Path, scratch: Path):
        self.origin, self.scratch, self.runs, self.finished = origin, scratch, [], True
        self.job_override: Callable[[list], None] | None = None
        self.pulls: list[dict] = []
        self.aggregate: tuple[str, str | None] | None = ("completed", "success")
        self.merge_error: str | None = None
        self.gh_calls: list[tuple] = []
        self.gh_fail: dict[str, str] = {}  # "comment" / "close" -> stderr of a failing gh pr <verb>
        self.on_close: Callable[[dict], None] | None = None
        self.canonical_owner = "example"  # a transferred repo: origin's slug owner != GitHub's real owner

    def ci(self):
        """Run 'CI' for every staged branch that has no run yet (status pending until finish())."""
        refs = git(self.origin, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/sync/")
        for line in filter(None, refs.splitlines()):
            branch, sha = line.split()
            if any(r["head_branch"] == branch for r in self.runs):
                continue
            work = self.scratch / f"ci-{sha[:10]}"
            subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
            git(work, "checkout", "-q", sha)
            res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                  "-c", "/dev/null", "--rootdir", str(work), str(work)],
                                 capture_output=True, text=True, cwd=work, check=False)
            run_id = len(self.runs) + 1
            self.runs.append({"id": run_id, "run_number": run_id, "head_sha": sha, "head_branch": branch,
                              "event": "push", "path": sync.WORKFLOW_PATH,
                              "status": "completed" if self.finished else "in_progress",
                              "conclusion": ("success" if res.returncode == 0 else "failure") if self.finished else None,
                              "html_url": f"https://ci.example/run/{run_id}",
                              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def branch_sha(self, branch):
        return subprocess.run(["git", "-C", str(self.origin), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                              capture_output=True, text=True, check=False).stdout.strip()

    def check_runs(self, sha):
        if self.aggregate is None or not any(p["head"]["sha"] == sha for p in self.pulls):
            return []
        status, conclusion = self.aggregate
        return [{"id": 7, "name": sync.REQUIRED_CHECK, "head_sha": sha, "status": status, "conclusion": conclusion,
                 "app": {"id": sync.ACTIONS_APP_ID}, "html_url": f"https://ci.example/check/{sha[:7]}"}]

    def request(self, endpoint):
        if "/jobs?" in endpoint:
            run = next(r for r in self.runs if f"/runs/{r['id']}/jobs" in endpoint)
            jobs = [{"name": f"Full suite / {n}", "status": "completed", "conclusion": run["conclusion"],
                     "labels": ["self-hosted", "hermes-hub"], "runner_name": "hub-1"} for n in sync.REQUIRED_JOBS]
            if self.job_override:
                self.job_override(jobs)
            return [{"jobs": jobs}]
        if "/pulls?" in endpoint:
            open_prs = [p for p in self.pulls if p["state"] == "open"]
            if "head=" not in endpoint:
                return [open_prs]
            owner, branch = endpoint.split("head=", 1)[1].split("&", 1)[0].split(":", 1)
            # GitHub filters by the PR's real (canonical) owner, not the slug origin happens to name
            return [[p for p in open_prs if p["head"]["ref"] == branch and owner == self.canonical_owner]]
        if "/check-runs?" in endpoint:
            assert f"check_name={sync.REQUIRED_CHECK.replace(' ', '%20')}" in endpoint
            return [{"check_runs": self.check_runs(endpoint.split("/commits/", 1)[1].split("/", 1)[0])}]
        assert f"workflows/{sync.WORKFLOW_FILE}/runs" in endpoint
        return [{"workflow_runs": list(reversed(self.runs))}]

    def gh(self, *args, reason=None):
        self.gh_calls.append(args)
        done = lambda rc, out="", err="": subprocess.CompletedProcess(["gh", *args], rc, out, err)
        if args[:2] == ("pr", "create"):
            branch = args[args.index("--head") + 1]
            number = len(self.pulls) + 1
            self.pulls.append({"number": number, "state": "open", "base": {"ref": args[args.index("--base") + 1]},
                               "head": {"ref": branch, "sha": self.branch_sha(branch),
                                        "repo": {"full_name": "example/fork"}},
                               "html_url": f"https://github.com/example/fork/pull/{number}", "comments": []})
            return done(0, f"https://github.com/example/fork/pull/{number}\n")
        pr = next(p for p in self.pulls if p["number"] == int(args[2]))
        if args[:2] in (("pr", "comment"), ("pr", "close")):
            assert args[3:5] == ("-R", "example/fork") and "--delete-branch" not in args, args
            if args[1] in self.gh_fail:
                return done(1, err=self.gh_fail[args[1]])
            if args[1] == "comment":
                pr["comments"].append(args[args.index("--body") + 1])
            else:
                pr["state"] = "closed"
                if self.on_close:
                    self.on_close(pr)
            return done(0)
        assert args[:2] == ("pr", "merge") and "--admin" not in args, args
        head = self.branch_sha(pr["head"]["ref"])
        if self.merge_error:
            return done(1, err=self.merge_error)
        if args[args.index("--match-head-commit") + 1] != head:
            return done(1, err="GraphQL: Head branch was modified. Review and try the merge again.")
        if [(r["status"], r["conclusion"]) for r in self.check_runs(head)] != [("completed", "success")]:
            return done(1, err=f"X Pull request example/fork#{pr['number']} is not mergeable: "
                               "the base branch policy prohibits the merge.")
        main = git(self.origin, "rev-parse", "refs/heads/main")
        tree = git(self.origin, "merge-tree", "--write-tree", main, head).splitlines()[0]
        env = {**os.environ, "GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@github.com",
               "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}
        merge = git(self.origin, "commit-tree", tree, "-p", main, "-p", head, "-m", f"Merge PR #{pr['number']}", env=env)
        git(self.origin, "update-ref", "refs/heads/main", merge, main)  # server-side: no pre-receive hook
        pr["state"] = "merged"
        return done(0, f"Merged pull request #{pr['number']}\n")


def protect_main(world_root, refs=("refs/heads/main",), code="GH006"):
    """GitHub's branch protection with enforce_admins: every direct push to ``refs`` is refused."""
    hook = world_root / "origin.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nstatus=0\nwhile read old new ref; do\n  case \" " + " ".join(refs) + " \" in\n"
                    "    *\" $ref \"*) echo \"remote: error: " + code + ": Protected branch update failed for $ref.\" >&2\n"
                    "      echo 'remote: error: Required status check \"All required checks pass (v2)\" is expected.' >&2\n"
                    "      echo 'remote: hint: " + "x" * 700 + "' >&2\n"  # GitHub's verdict is never the tail
                    "      status=1 ;;\n  esac\ndone\nexit $status\n")
    hook.chmod(0o755)


def gate(checkout, head, actions, **kw):
    return sync.advance(checkout, head, gh_repo="example/fork", request=actions.request, gh=actions.gh, **kw)


def main_of(world_root):
    return git(world_root / "origin.git", "rev-parse", "main")


def landed_by_merge(world_root, head, before):
    """GitHub's PR merge: a merge commit (main as it was, the exact tested head) and nothing else."""
    tip = main_of(world_root)
    return git(world_root / "origin.git", "rev-list", "--parents", "-n1", tip).split()[1:] == [before, head]


@pytest.mark.parametrize("red", [True, False])
def test_red_sync_is_refused_and_green_sync_lands(world, red):
    root, checkout = world
    upstream_commit(root, {"test_upstream.py": f"def test_upstream(): assert {not red}\n"}, "upstream change")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = FakeActions(root / "origin.git", root)

    assert gate(checkout, head, actions) == sync.NOT_YET  # staged, not landed
    assert main_of(root) == before
    assert git(root / "origin.git", "rev-parse", f"sync/upstream-{head}") == head
    actions.ci()
    assert actions.runs[0]["conclusion"] == ("failure" if red else "success")  # real pytest verdict

    rc = gate(checkout, head, actions)
    assert rc == (sync.REFUSED if red else sync.LANDED)
    if red:
        assert main_of(root) == before
    else:  # green lands the exact tested head through its PR's merge commit
        assert landed_by_merge(root, head, before)


def test_pending_run_does_not_land_then_lands_when_green(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = FakeActions(root / "origin.git", root)
    actions.finished = False
    gate(checkout, head, actions)
    actions.ci()
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert main_of(root) == before
    actions.runs[0].update(status="completed", conclusion="success")
    assert gate(checkout, head, actions) == sync.LANDED
    assert landed_by_merge(root, head, before)


def test_moved_main_gets_a_non_forced_merge_of_the_tested_head(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()
    # The fork's main moves (a fork-only fix) while the suite runs.
    other = root / "other"
    subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(other)], check=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "O")
    moved = commit(other, {"fork_fix.txt": "fix\n"}, "fork fix")
    git(other, "push", "-q", "origin", "main")

    assert gate(checkout, head, actions) == sync.LANDED
    new_main = main_of(root)
    parents = git(root / "origin.git", "rev-list", "--parents", "-n1", new_main).split()[1:]
    assert parents == [moved, head]  # main kept, tested head merged, nothing rewritten


def test_migrated_blacksmith_full_suite_can_land(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()

    def migrated(jobs):
        for job in jobs:
            job["labels"] = ["blacksmith-4vcpu-ubuntu-2404"]
            job["runner_name"] = "blacksmith-4vcpu-ubuntu-2404-fixture"
    actions.job_override = migrated
    assert gate(checkout, head, actions) == sync.LANDED
    assert landed_by_merge(root, head, before)


@pytest.mark.parametrize("fault", ["missing", "skipped", "hosted", "no-runner", "unknown-blacksmith"])
def test_incomplete_job_evidence_cannot_land(world, fault):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()

    def corrupt(jobs):
        if fault == "missing":
            jobs.pop()
        elif fault == "skipped":
            jobs[-1]["conclusion"] = "skipped"
        elif fault == "hosted":
            jobs[-1]["labels"] = ["ubuntu-latest"]
        elif fault == "unknown-blacksmith":
            jobs[-1]["labels"] = ["blacksmith-unapproved"]
        else:
            jobs[-1]["runner_name"] = None
    actions.job_override = corrupt
    assert gate(checkout, head, actions) == sync.REFUSED
    assert main_of(root) == before


def test_evidence_from_another_branch_or_cancelled_run_does_not_count(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    actions.runs.append({"id": 9, "run_number": 9, "head_sha": head, "head_branch": "main", "event": "push",
                         "path": sync.WORKFLOW_PATH, "status": "completed", "conclusion": "success",
                         "html_url": "x", "updated_at": "2026-10-30T00:00:00Z"})
    actions.runs.append({"id": 10, "run_number": 10, "head_sha": head, "head_branch": f"sync/upstream-{head}",
                         "event": "push", "path": sync.WORKFLOW_PATH, "status": "completed",
                         "conclusion": "cancelled", "html_url": "y", "updated_at": "2026-10-30T00:00:00Z"})
    assert gate(checkout, head, actions) == sync.NOT_YET  # neither counts: staged for a real run
    assert main_of(root) != head


def staged(root, sha):
    return bool(git(root / "origin.git", "branch", "--list", f"sync/upstream-{sha}"))


def fork_commit_on_main(root, name):
    other = root / f"fork-{name}"
    subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(other)], check=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "O")
    commit(other, {name: "fork\n"}, f"fork {name}")
    git(other, "push", "-q", "origin", "main")


def test_one_candidate_in_flight_and_red_backoff_per_upstream(world):
    root, checkout = world
    upstream_commit(root, {"test_u.py": "def test_u(): assert False\n"}, "broken upstream")
    red_head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    actions.finished = False
    gate(checkout, red_head, actions)
    actions.ci()
    fork_commit_on_main(root, "a.txt")
    same_upstream = merge_upstream(checkout)  # main moved, same broken upstream
    assert gate(checkout, same_upstream, actions) == sync.NOT_YET
    assert not staged(root, same_upstream)  # one candidate in CI at a time

    actions.runs[0].update(status="completed", conclusion="failure")
    assert gate(checkout, same_upstream, actions) == sync.NOT_YET  # red backoff: same upstream
    assert not staged(root, same_upstream)
    later = time.time() + sync.RED_BACKOFF_S + 60
    assert gate(checkout, same_upstream, actions, now=later) == sync.NOT_YET
    assert staged(root, same_upstream)  # staged after the backoff

    upstream_commit(root, {"more.txt": "m\n"}, "more upstream")
    new_upstream = merge_upstream(checkout)
    assert gate(checkout, new_upstream, actions) == sync.NOT_YET
    assert staged(root, new_upstream)  # upstream moved: no backoff


def test_another_hosts_green_candidate_is_landed_by_a_bare_call(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()
    assert sync.advance(checkout, None, gh_repo="example/fork", request=actions.request, gh=actions.gh) == sync.LANDED
    assert landed_by_merge(root, head, before)


def test_unreadable_evidence_is_a_refusal(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)

    def broken(endpoint):
        raise sync.GateError("API down")
    assert sync.advance(checkout, head, gh_repo="example/fork", request=broken) == sync.REFUSED
    assert main_of(root) == before


def green_candidate(world):
    """A staged sync whose full suite went green, on an origin whose main refuses direct pushes."""
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    assert gate(checkout, head, actions) == sync.NOT_YET  # staged
    actions.ci()
    protect_main(root)
    return root, checkout, head, actions, main_of(root)


def merges(actions):
    return [c for c in actions.gh_calls if c[:2] == ("pr", "merge")]


def test_protected_main_lands_through_a_pr_never_a_direct_push(world, capsys):
    root, checkout, head, actions, before = green_candidate(world)
    assert gate(checkout, head, actions) == sync.LANDED, capsys.readouterr().out
    assert landed_by_merge(root, head, before)
    [pr] = actions.pulls
    assert pr["head"]["ref"] == f"sync/upstream-{head}" and pr["head"]["sha"] == head and pr["base"]["ref"] == "main"
    [merge] = merges(actions)
    assert merge[merge.index("--match-head-commit") + 1] == head and "--merge" in merge and "--admin" not in merge
    assert not git(root / "origin.git", "branch", "--list", f"sync/upstream-{head}")  # cleaned up


@pytest.mark.parametrize("aggregate,expected", [
    (("completed", "success"), sync.LANDED),
    (("in_progress", None), sync.NOT_YET),
    (("queued", None), sync.NOT_YET),
    (None, sync.NOT_YET),  # ci.yaml has not reported on the head yet
    (("completed", "failure"), sync.REFUSED),
    (("completed", "cancelled"), sync.REFUSED),
])
def test_landing_pr_merges_only_on_a_green_required_check_at_the_exact_head(world, capsys, aggregate, expected):
    root, checkout, head, actions, before = green_candidate(world)
    actions.aggregate = aggregate
    assert gate(checkout, head, actions) == expected
    out = capsys.readouterr().out
    assert "main moved" not in out
    if expected == sync.LANDED:
        assert landed_by_merge(root, head, before) and len(merges(actions)) == 1
        return
    assert main_of(root) == before and merges(actions) == [], out
    assert sync.REQUIRED_CHECK in out and "PR #1" in out
    if expected == sync.NOT_YET:  # the next call reuses the PR and merges once the check turns green
        actions.aggregate = ("completed", "success")
        assert gate(checkout, head, actions) == sync.LANDED
        assert landed_by_merge(root, head, before) and len(actions.pulls) == 1


def test_an_open_landing_pr_is_reused_when_origin_names_a_transferred_repo(world, capsys):
    """origin daveey/hermes redirects to softmax-universe/daveey-hermes: an ``head=<slug owner>:`` filter misses
    the open PR, and re-creating it fails with "already exists" on every run, so nothing ever lands."""
    root, checkout, head, actions, before = green_candidate(world)
    actions.aggregate = ("in_progress", None)
    assert gate(checkout, head, actions) == sync.NOT_YET  # opens the landing PR
    actions.canonical_owner = "new-owner"  # the repo moved; origin's URL still says example/fork
    actions.aggregate = ("completed", "success")
    assert gate(checkout, head, actions) == sync.LANDED, capsys.readouterr().out
    assert landed_by_merge(root, head, before) and len(actions.pulls) == 1
    assert len([c for c in actions.gh_calls if c[:2] == ("pr", "create")]) == 1


GH006 = ("remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
         "remote: error: Required status check \"All required checks pass (v2)\" is expected.")


def test_a_gh006_refusal_is_refused_with_githubs_message_not_main_moved(world, capsys):
    root, checkout, head, actions, before = green_candidate(world)
    actions.merge_error = GH006  # GitHub refuses the merge itself (e.g. the check went stale meanwhile)
    assert gate(checkout, head, actions) == sync.REFUSED
    out = capsys.readouterr().out
    assert "REFUSED" in out and "GH006: Protected branch update failed" in out and "main moved" not in out
    assert main_of(root) == before


def test_a_protected_staging_branch_is_refused_with_githubs_message(world, capsys):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    protect_main(root, refs=("refs/heads/main", f"refs/heads/sync/upstream-{head}"), code="GH013")
    assert gate(checkout, head, FakeActions(root / "origin.git", root)) == sync.REFUSED
    out = capsys.readouterr().out
    assert "GitHub refused the push to refs/heads/sync/upstream-" in out and "GH013" in out


@pytest.mark.parametrize("message,refusal", [
    (GH006, True),
    ("remote: error: GH013: Repository rule violations found for refs/heads/main.", True),
    ("X Pull request daveey/hermes#12 is not mergeable: the base branch policy prohibits the merge.", True),
    ("! [rejected]        x -> main (fetch first)", False),
    ("fatal: unable to access 'https://github.com/daveey/hermes.git/': Could not resolve host", False),
])
def test_protection_refusals_are_recognized(message, refusal):
    assert sync.is_protection_refusal(message) is refusal


def install_guard(checkout):
    hook = checkout / ".git" / "hooks" / "pre-push"
    script = Path(sync.__file__).resolve()
    hook.write_text("#!/usr/bin/env bash\nstatus=0\nwhile read -r _ l r rs; do\n"
                    "  [ \"$r\" = refs/heads/main ] || continue\n"
                    f"  {shlex.quote(sys.executable)} {shlex.quote(str(script))} --repo . --guard-push \"$l\" || status=1\n"
                    "done\nexit $status\n")
    hook.chmod(0o755)


def test_pre_push_guard_refuses_a_hand_pushed_sync_and_allows_fork_work(world):
    root, checkout = world
    install_guard(checkout)
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    res = subprocess.run(["git", "-C", str(checkout), "push", "origin", f"{head}:refs/heads/main"],
                         capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "upstream sync guard" in res.stderr
    assert main_of(root) != head
    # The same sync, through the gate's publication, is allowed.
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()
    assert gate(checkout, head, actions) == sync.LANDED
    # Fork-only work brings no new upstream commits and pushes normally.
    git(checkout, "checkout", "-q", "--detach", main_of(root))
    fork = commit(checkout, {"fork.txt": "f\n"}, "fork work")
    git(checkout, "push", "-q", "origin", f"{fork}:refs/heads/main")
    assert main_of(root) == fork


def test_github_transport_reads_all_pages(monkeypatch):
    monkeypatch.setattr(sync.shutil, "which", lambda *a, **kw: "gh")

    def run(command, **kwargs):
        assert command[:4] == ["gh", "api", "--paginate", "--slurp"]
        return subprocess.CompletedProcess(command, 0, json.dumps([{"jobs": []}, {"jobs": []}]), "")
    monkeypatch.setattr(subprocess, "run", run)
    assert len(sync.github_pages("repos/example/fork/actions/runs/1/jobs?per_page=100")) == 2


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("broker", [False, True])
def test_github_transport_finds_home_bin_with_updater_path(tmp_path, monkeypatch, broker):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    which = sync.shutil.which
    monkeypatch.setattr(sync.shutil, "which",
                        lambda name, **kw: None if name == "gh" and not kw else which(name, **kw))
    executable(tmp_path / "bin/gh", "import os, sys\n"
               + ("if not os.environ.get('BROKER_TEST'): sys.exit(1)\n" if broker else "")
               + "print('[{\"jobs\": []}, {\"jobs\": []}]')\n")
    if broker:
        executable(tmp_path / ".local/bin/token-broker",
                   "import os, sys\nos.environ['BROKER_TEST']='1'\n"
                   "args=sys.argv[sys.argv.index('--')+1:]\nos.execv(args[0], args)\n")
    assert sync.github_pages("repos/example/fork/jobs") == [{"jobs": []}, {"jobs": []}]


def test_github_transport_missing_locally_can_use_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sync.shutil, "which", lambda *a, **kw: None)
    executable(tmp_path / ".local/bin/token-broker",
               "import sys\nassert sys.argv[sys.argv.index('--')+1:] == "
               "['gh', 'api', '--paginate', '--slurp', 'repos/example/fork/jobs']\n"
               "print('[{\"jobs\": []}]')\n")
    assert sync.github_pages("repos/example/fork/jobs") == [{"jobs": []}]


def test_github_transport_missing_everywhere_refuses_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sync.shutil, "which", lambda *a, **kw: None)
    assert sync.advance(tmp_path, None, gh_repo="example/fork", request=sync.github_pages) == sync.REFUSED
    output = capsys.readouterr().out
    assert "REFUSED:" in output and "gh not found" in output
    assert "Traceback" not in output
