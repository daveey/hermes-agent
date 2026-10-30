"""Upstream sync gate: real bare Git remotes and real pytest verdicts; only GitHub's API is a fixture.

The fixture API reports for each staged ``sync/upstream-<sha>`` branch the verdict a real pytest run
on that exact checkout produced, so "red is refused, green lands" is decided by real test code.
"""
import json
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
    """GitHub Actions for the bare origin: a staged sync branch runs real pytest at its head."""

    def __init__(self, origin: Path, scratch: Path):
        self.origin, self.scratch, self.runs, self.finished = origin, scratch, [], True
        self.job_override: Callable[[list], None] | None = None

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

    def request(self, endpoint):
        if "/jobs?" in endpoint:
            run = next(r for r in self.runs if f"/runs/{r['id']}/jobs" in endpoint)
            jobs = [{"name": f"Full suite / {n}", "status": "completed", "conclusion": run["conclusion"],
                     "labels": ["self-hosted", "hermes-hub"], "runner_name": "hub-1"} for n in sync.REQUIRED_JOBS]
            if self.job_override:
                self.job_override(jobs)
            return [{"jobs": jobs}]
        assert f"workflows/{sync.WORKFLOW_FILE}/runs" in endpoint
        return [{"workflow_runs": list(reversed(self.runs))}]


def gate(checkout, head, actions, **kw):
    return sync.advance(checkout, head, gh_repo="example/fork", request=actions.request, **kw)


def main_of(world_root):
    return git(world_root / "origin.git", "rev-parse", "main")


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
    assert main_of(root) == (before if red else head)  # green lands the exact tested head (ff)


def test_pending_run_does_not_land_then_lands_when_green(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    actions.finished = False
    gate(checkout, head, actions)
    actions.ci()
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert main_of(root) != head
    actions.runs[0].update(status="completed", conclusion="success")
    assert gate(checkout, head, actions) == sync.LANDED
    assert main_of(root) == head


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


@pytest.mark.parametrize("fault", ["missing", "skipped", "hosted", "no-runner"])
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
    actions = FakeActions(root / "origin.git", root)
    gate(checkout, head, actions)
    actions.ci()
    assert sync.advance(checkout, None, gh_repo="example/fork", request=actions.request) == sync.LANDED
    assert main_of(root) == head


def test_unreadable_evidence_is_a_refusal(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    head = merge_upstream(checkout)
    before = main_of(root)

    def broken(endpoint):
        raise sync.GateError("API down")
    assert sync.advance(checkout, head, gh_repo="example/fork", request=broken) == sync.REFUSED
    assert main_of(root) == before


def install_guard(checkout):
    hook = checkout / ".git" / "hooks" / "pre-push"
    script = Path(sync.__file__).resolve()
    hook.write_text("#!/usr/bin/env bash\nstatus=0\nwhile read -r _ l r rs; do\n"
                    "  [ \"$r\" = refs/heads/main ] || continue\n"
                    f"  {sys.executable} {script} --repo . --guard-push \"$l\" || status=1\n"
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
