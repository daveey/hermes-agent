"""Bounded read-only PR observations for the lane-flow report. [daveey]"""
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from tools.environments.local import build_subprocess_env

# Brokered gh calls measure 3-4.5 s each on the fleet, so the budget is wall-clock for the whole
# report and PRs are read concurrently (``prefetch``), not as one call chain after another.
BUDGET_S = 30
_VIEW = "state,headRefOid,reviewDecision,author,reviews"


def _login(person):
    return str(person.get("login") or "").lower() if isinstance(person, dict) else ""


def _bot(person):
    return isinstance(person, dict) and bool(person.get("is_bot") or _login(person).endswith("[bot]"))


def independent_approval(data):
    """True when each non-author human's latest verdict is clean and one approves the current head.

    ``reviewDecision`` is empty on repositories that require no PR reviews, so it alone never shows approval.
    """
    head = data.get("headRefOid")
    latest = {}
    for review in data.get("reviews") or ():
        if not isinstance(review, dict):
            continue
        who = _login(review.get("author"))
        if not who or _bot(review.get("author")) or who == _login(data.get("author")):
            continue
        if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[who] = review
    if not latest or any(r.get("state") == "CHANGES_REQUESTED" for r in latest.values()):
        return False
    return any(r.get("state") == "APPROVED" and (r.get("commit") or {}).get("oid") == head
               for r in latest.values())


class Reader:
    """One report has a bounded network budget; unknown evidence stays unclassified."""

    def __init__(self, budget=BUDGET_S):
        self.deadline = time.monotonic() + budget
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

    def prefetch(self, urls):
        """Warm the cache for every URL concurrently; ``get`` then answers from memory."""
        todo = sorted({u for u in urls if u and u not in self.cache})
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                list(pool.map(self.get, todo))

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        self.cache[url] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            view = pool.submit(self._read, ["pr", "view", url, "--json", _VIEW])
            checks = pool.submit(self._read, ["pr", "checks", url, "--required", "--json", "bucket"])
            data, checks = view.result(), checks.result()
        if not isinstance(data, dict):
            return {}
        state = data.get("state")
        out = {"merged": state == "MERGED"} if state in ("OPEN", "MERGED") else {}
        if state == "OPEN":
            out["approved"] = data.get("reviewDecision") == "APPROVED" or independent_approval(data)
            confirm = self._read(["pr", "view", url, "--json", "state,headRefOid,reviewDecision"])
            if (not isinstance(confirm, dict) or not data.get("headRefOid")
                    or any(confirm.get(k) != data.get(k) for k in ("state", "headRefOid", "reviewDecision"))):
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
