"""A real fork gate for updater tests: GitHub-named bare origin plus a fake ``gh`` on PATH.

The bare origin answers to ``https://github.com/example/fork.git`` (``url.<path>.insteadOf``), so
``scripts/ci/upstream_sync.py`` runs unmodified in a subprocess: real git pushes, and Actions runs
served by a ``gh`` stub from a JSON file the test writes.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "ci" / "upstream_sync.py"
URL = "https://github.com/example/fork.git"

_GH = r'''#!{py}
import json, os, subprocess, sys
state = {state!r}
bare = {bare!r}
runs = json.load(open(state + "/runs.json"))
pulls = json.load(open(state + "/pulls.json"))
checks = json.load(open(state + "/checks.json"))
args = sys.argv[1:]

def git(*a, env=None):
    return subprocess.run(["git", "-C", bare, *a], check=True, capture_output=True, text=True, env=env).stdout.strip()

def branch_sha(branch):
    return subprocess.run(["git", "-C", bare, "rev-parse", "-q", "--verify", "refs/heads/" + branch],
                          capture_output=True, text=True).stdout.strip()

if args[:2] == ["pr", "create"]:
    branch = args[args.index("--head") + 1]
    number = len(pulls) + 1
    pulls.append({{"number": number, "state": "open", "base": {{"ref": args[args.index("--base") + 1]}},
                  "head": {{"ref": branch, "sha": branch_sha(branch)}},
                  "html_url": f"https://github.com/example/fork/pull/{{number}}"}})
    json.dump(pulls, open(state + "/pulls.json", "w"))
    print(f"https://github.com/example/fork/pull/{{number}}")
    sys.exit(0)
if args[:2] == ["pr", "merge"]:
    assert "--admin" not in args, args
    pr = next(p for p in pulls if p["number"] == int(args[2]))
    head = branch_sha(pr["head"]["ref"])
    if args[args.index("--match-head-commit") + 1] != head or checks.get(head) != ["completed", "success"]:
        print("X Pull request is not mergeable: the base branch policy prohibits the merge.", file=sys.stderr)
        sys.exit(1)
    main = git("rev-parse", "refs/heads/main")
    tree = git("merge-tree", "--write-tree", main, head).splitlines()[0]
    env = {{**os.environ, "GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@github.com",
           "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}}
    merge = git("commit-tree", tree, "-p", main, "-p", head, "-m", "Merge PR", env=env)
    git("update-ref", "refs/heads/main", merge, main)
    pr["state"] = "merged"
    json.dump(pulls, open(state + "/pulls.json", "w"))
    sys.exit(0)
endpoint = [a for a in args if not a.startswith("-")][-1]
if "/jobs" in endpoint:
    run = next(r for r in runs if f"/runs/{{r['id']}}/jobs" in endpoint)
    jobs = [{{"name": "Full suite / " + n, "status": "completed", "conclusion": run["conclusion"],
              "labels": ["self-hosted", "hermes-hub"], "runner_name": "hub-1"}}
            for n in [f"Run tests ({{i}}/8)" for i in range(1, 9)] + ["e2e"]]
    print(json.dumps([{{"jobs": jobs}}]))
elif "/pulls?" in endpoint:
    branch = endpoint.split("head=", 1)[1].split("&", 1)[0].split(":", 1)[1] if "head=" in endpoint else None
    print(json.dumps([[p for p in pulls if p["state"] == "open" and branch in (None, p["head"]["ref"])]]))
elif "/check-runs?" in endpoint:
    sha = endpoint.split("/commits/", 1)[1].split("/", 1)[0]
    verdict = checks.get(sha) if any(p["head"]["sha"] == sha for p in pulls) else None
    print(json.dumps([{{"check_runs": [{{"id": 1, "name": "All required checks pass (v2)", "head_sha": sha,
                                       "status": verdict[0], "conclusion": verdict[1],
                                       "app": {{"id": 15368}}}}] if verdict else []}}]))
else:
    page = {{"workflow_runs": runs}}
    print(json.dumps([page] if "--paginate" in args else page))  # a named page= is one page, not a list
'''


class FakeFork:
    """The fork on GitHub: Actions runs, landing PRs and their required check, from JSON files the test writes.

    ``finish(sha, "success")`` also turns ci.yaml's required check green on ``sha`` (the landing PR's
    head), so a green candidate lands through its PR in one gate call, the way it does once both
    workflows are done. ``gh pr merge`` writes GitHub's merge commit (main, head) into the bare repo.
    """

    def __init__(self, tmp_path: Path, monkeypatch, bare: Path):
        self.bare, self.state = bare, tmp_path / "gh-state"
        self.state.mkdir(exist_ok=True)
        self.runs_file = self.state / "runs.json"
        for name in ("runs.json", "pulls.json"):
            (self.state / name).write_text("[]")
        (self.state / "checks.json").write_text("{}")
        bindir = tmp_path / "fake-bin"
        bindir.mkdir(exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(_GH.format(py=sys.executable, state=str(self.state), bare=str(bare)))
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("HOME", str(tmp_path))  # no real token-broker fallback, no global git config
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / ".gitconfig"))
        (tmp_path / ".gitconfig").write_text(
            f'[user]\n\temail = t@t\n\tname = t\n[url "{bare}"]\n\tinsteadOf = {URL}\n')

    @staticmethod
    def adopt(clone: Path) -> None:
        """Point ``clone``'s origin at the GitHub-named URL (resolved to the bare repo) and give it the gate."""
        subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", URL], check=True)
        gate = clone / "scripts" / "ci" / "upstream_sync.py"
        gate.parent.mkdir(parents=True, exist_ok=True)
        for sibling in GATE.parent.glob("upstream_sync*.py"):  # the gate and the helper modules it imports
            (gate.parent / sibling.name).write_bytes(sibling.read_bytes())
        with open(clone / ".git" / "info" / "exclude", "a") as exclude:
            exclude.write("/scripts/\n")

    def staged(self) -> list[str]:
        out = subprocess.run(["git", "-C", str(self.bare), "for-each-ref", "--format=%(objectname)",
                              "refs/heads/sync/"], check=True, capture_output=True, text=True).stdout
        return out.split()

    def finish(self, sha: str, conclusion: str) -> None:
        runs = json.loads(self.runs_file.read_text())
        runs.append({"id": len(runs) + 1, "run_number": len(runs) + 1, "head_sha": sha,
                     "head_branch": f"sync/upstream-{sha}", "event": "push",
                     "path": ".github/workflows/upstream-sync.yml", "status": "completed",
                     "conclusion": conclusion, "html_url": f"https://ci.example/{len(runs) + 1}",
                     "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.runs_file.write_text(json.dumps(runs))
        if conclusion == "success":
            checks = json.loads((self.state / "checks.json").read_text())
            checks[sha] = ["completed", "success"]
            (self.state / "checks.json").write_text(json.dumps(checks))

    def landed(self, sha: str) -> bool:
        """``sha`` reached main through its PR: main is GitHub's merge commit whose second parent is ``sha``."""
        return subprocess.run(["git", "-C", str(self.bare), "rev-parse", "refs/heads/main^2"], check=False,
                              capture_output=True, text=True).stdout.strip() == sha

    def main(self) -> str:
        return subprocess.run(["git", "-C", str(self.bare), "rev-parse", "refs/heads/main"], check=True,
                              capture_output=True, text=True).stdout.strip()
