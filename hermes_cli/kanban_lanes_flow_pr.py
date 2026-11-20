"""Bounded read-only PR observations for the lane-flow report. [daveey]"""
import json
import subprocess
import time

from tools.environments.local import build_subprocess_env


class Reader:
    """One report has a five-second network budget; unknown evidence stays unclassified."""

    def __init__(self):
        self.deadline = time.monotonic() + 5
        self.cache = {}

    def _read(self, argv):
        from hermes_cli.kanban_db_dispatch_pr_state import _gh_argv

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        gh = _gh_argv()
        if not gh:
            return None
        try:
            result = subprocess.run(
                [*gh, *argv], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=remaining,
                env=build_subprocess_env(), check=False)
            return json.loads(result.stdout) if result.returncode in (0, 8) else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        self.cache[url] = {}
        data = self._read(["pr", "view", url, "--json", "state,headRefOid,reviewDecision"])
        if not isinstance(data, dict):
            return {}
        state = data.get("state")
        out = {"merged": state == "MERGED"} if state in ("OPEN", "MERGED") else {}
        if state == "OPEN":
            out["approved"] = data.get("reviewDecision") == "APPROVED"
            checks = self._read(["pr", "checks", url, "--required", "--json", "bucket"])
            confirm = self._read(["pr", "view", url, "--json", "state,headRefOid,reviewDecision"])
            if (not isinstance(confirm, dict) or not data.get("headRefOid")
                    or any(confirm.get(k) != data.get(k) for k in ("state", "headRefOid", "reviewDecision"))):
                self.cache[url] = {}
                return {}
            # Empty/missing required checks never establish green.
            if isinstance(checks, list) and checks:
                buckets = [c.get("bucket") for c in checks if isinstance(c, dict)]
                if len(buckets) == len(checks) and all(b == "pass" for b in buckets):
                    out["checks"] = "green"
                elif "pending" in buckets:
                    out["checks"] = "pending"
        self.cache[url] = out
        return out
