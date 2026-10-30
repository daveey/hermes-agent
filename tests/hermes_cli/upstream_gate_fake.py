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
import json, sys
endpoint = [a for a in sys.argv[1:] if not a.startswith("-")][-1]
runs = json.load(open({runs!r}))
if "/jobs" in endpoint:
    run = next(r for r in runs if f"/runs/{{r['id']}}/jobs" in endpoint)
    jobs = [{{"name": "Full suite / " + n, "status": "completed", "conclusion": run["conclusion"],
              "labels": ["self-hosted", "hermes-hub"], "runner_name": "hub-1"}}
            for n in ("Run tests (1/2)", "Run tests (2/2)", "e2e")]
    print(json.dumps([{{"jobs": jobs}}]))
else:
    print(json.dumps([{{"workflow_runs": runs}}]))
'''


class FakeFork:
    def __init__(self, tmp_path: Path, monkeypatch, bare: Path):
        self.bare, self.runs_file = bare, tmp_path / "gh-runs.json"
        self.runs_file.write_text("[]")
        bindir = tmp_path / "fake-bin"
        bindir.mkdir(exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(_GH.format(py=sys.executable, runs=str(self.runs_file)))
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
        gate.write_bytes(GATE.read_bytes())
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

    def main(self) -> str:
        return subprocess.run(["git", "-C", str(self.bare), "rev-parse", "refs/heads/main"], check=True,
                              capture_output=True, text=True).stdout.strip()
