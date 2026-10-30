#!/usr/bin/env python3
"""Nightly e2e on main: dispatch .github/workflows/nightly-e2e.yml and report the result on the Kanban board.

The fork's e2e suite sat red on main for days because no workflow ran it on main and nothing watched it; the
upstream-sync gate then refused every sync for a cause that was not upstream's. This is the watcher. It runs on
hermes-hub from two Hermes cron jobs (never from a laptop, never a GitHub ``schedule:``):

  nightly_e2e.py dispatch [--ref main]   02:00 PT: resolve the ref to a SHA, dispatch nightly-e2e.yml, record the run
  nightly_e2e.py report                  every 10 min: settle each recorded run, file/update/comment the board

Reporting (``report``), always within ``DEADLINE_HOURS`` of the dispatch:

* failed      -> ONE Kanban card per distinct set of failing test ids (sha1 of the sorted ids). A later failing run
                 with the same ids comments on that open card instead of filing another. Body: run link + tests.
* not finished in time / cancelled / never started -> ONE "did not run" card, later occurrences comment on it.
* green while a card of ours is open -> comment the green run on every open one.
* GitHub (or the board) keeps erroring -> retried every tick; past the deadline the run gets the "did not run" card
                 with the error. If the board itself fails, the run stays pending and ``report`` exits 1, so the cron
                 run shows the error; a run is never dropped without a card.

Both commands hold an exclusive flock on ``<state>.lock`` from loading the state to saving it, so the 02:00 dispatch
and a report tick that overlap cannot overwrite each other's ``pending`` (hub-only, so fcntl is enough).

Never the Inbox: an agent can fix a red suite, so it is a card (AGENTS.md "Proposing Work").
Cards go through ``hermes kanban`` of the host it runs on (flaky_kanban.CliBoard); GitHub through the REST API
with ``GH_TOKEN``. The hub cron wrappers (~/.hermes/scripts/nightly_e2e_dispatch.py and nightly_e2e_report.py) run
this under ``token-broker exec --scope user/github``, which injects it: dispatching needs actions:write, which the
read-only GitHub App token ci_flaky_cards.py uses does not have.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flaky_kanban import OPEN, CliBoard, CreateRefused

REPO = "daveey/hermes"
WORKFLOW = "nightly-e2e.yml"
E2E_JOB = "e2e"  # the called tests.yml job: reported by GitHub as "e2e / e2e"
DEADLINE_HOURS = 4.0
RUN_LOOKUP_MINUTES = 30.0  # a dispatch with no run after this long never started
DEFAULT_STATE = Path(os.path.expanduser("~/.hermes/state/nightly-e2e.json"))
# pytest's short-summary lines: FAILED, and ERROR for setup/teardown/collection errors ("ERROR tests/x.py" has no
# "::"). The live "║ FAILED" copies are excluded.
FAILED_LINE = re.compile(r"Z (?:FAILED|ERROR) (tests/\S+)")
SUMMARY_LINE = re.compile(r"=== Summary: .*? ===")
MAX_LISTED = 150
# GitHub (urllib: OSError, bad JSON: ValueError, missing field: KeyError) and board (CliBoard: RuntimeError, a hung
# `hermes kanban`: subprocess.TimeoutExpired) failures; anything else is a bug and crashes.
SETTLE_ERRORS = (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError)
BOARD_FAILED = "BOARD FAILED"


class GitHub(Protocol):
    def dispatch(self, ref: str, sha: str) -> None: ...
    def resolve(self, ref: str) -> str: ...
    def runs_since(self, since: float) -> list[dict[str, Any]]: ...
    def run(self, run_id: int) -> dict[str, Any]: ...
    def jobs(self, run_id: int) -> list[dict[str, Any]]: ...
    def job_log(self, job_id: int) -> str: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
        return None


class RestGitHub:
    def __init__(
        self, token: str, repo: str = REPO, workflow_ref: str = "main"
    ) -> None:
        self.token, self.repo, self.workflow_ref = token, repo, workflow_ref
        self.api = f"https://api.github.com/repos/{repo}"

    def _req(
        self, url: str, data: dict | None = None, auth: bool = True
    ) -> urllib.request.Request:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-nightly-e2e",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(
            url,
            data=None if data is None else json.dumps(data).encode(),
            headers=headers,
        )

    def _json(self, path: str) -> Any:
        with urllib.request.urlopen(self._req(f"{self.api}{path}"), timeout=60) as resp:
            return json.loads(resp.read() or b"null")

    def resolve(self, ref: str) -> str:
        return str(self._json(f"/commits/{ref}")["sha"])

    def dispatch(self, ref: str, sha: str) -> None:
        req = self._req(
            f"{self.api}/actions/workflows/{WORKFLOW}/dispatches",
            {"ref": self.workflow_ref, "inputs": {"ref": sha}},
        )
        urllib.request.urlopen(req, timeout=60).read()

    def runs_since(self, since: float) -> list[dict[str, Any]]:
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        data = self._json(
            f"/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=50"
            f"&created=%3E%3D{created}"
        )
        return list(data.get("workflow_runs", []))

    def run(self, run_id: int) -> dict[str, Any]:
        return dict(self._json(f"/actions/runs/{run_id}"))

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return list(
            self._json(f"/actions/runs/{run_id}/jobs?per_page=100").get("jobs", [])
        )

    def job_log(self, job_id: int) -> str:
        # The log endpoint 302s to a signed blob URL that must be fetched WITHOUT the GitHub Authorization header.
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(
                self._req(f"{self.api}/actions/jobs/{job_id}/logs"), timeout=120
            ) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code not in (301, 302, 303, 307, 308) or not err.headers.get(
                "Location"
            ):
                raise
            with urllib.request.urlopen(
                self._req(err.headers["Location"], auth=False), timeout=300
            ) as resp:
                return resp.read().decode("utf-8", "replace")


class NightlyBoard(CliBoard):
    def create(
        self,
        title: str,
        body: str,
        key: str,
        *,
        distinct_from: list[str] | None = None,
        distinct_reason: str | None = None,
    ) -> str:
        override: list[str] = []
        for card_id in distinct_from or []:
            override += ["--distinct-from", card_id]
        if override:
            override += [
                "--distinct-reason",
                distinct_reason or "a separate nightly e2e finding",
            ]
        out = self._run(
            "create",
            title,
            "--assignee",
            self.assignee,
            "--idempotency-key",
            key,
            "--created-by",
            "nightly-e2e",
            "--body-file",
            "-",
            "--json",
            *override,
            stdin=body,
        )
        data = json.loads(out)
        return str(data.get("id") or data.get("task_id") or data["task"]["id"])


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("pending", [])  # dispatched runs not yet reported
    state.setdefault(
        "seen", []
    )  # run ids ever recorded: a clock-skewed lookup never adopts one twice
    state.setdefault(
        "cards", {}
    )  # key -> {"id": card id, "n": cards filed for this key}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked_state(path: Path) -> Iterator[dict[str, Any]]:
    """The state, loaded and saved under an exclusive flock on ``<path>.lock`` held for the whole block."""
    import fcntl  # hub-only; imported here so the module (and its tests) still import on Windows

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes, after save_state
        state = load_state(path)
        try:
            yield state
        finally:
            save_state(path, state)


def failing_tests(log: str) -> list[str]:
    """Sorted, de-duplicated test ids from a job log's pytest FAILED/ERROR summary lines (not the live ║ copies)."""
    return sorted(set(FAILED_LINE.findall(log)))


def failure_key(ids: list[str]) -> str:
    return "failed-" + hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()[:12]


def dispatch(
    gh: GitHub,
    state: dict[str, Any],
    ref: str,
    *,
    now: float,
    sleep=time.sleep,
    lookup_seconds: float = 120.0,
) -> dict[str, Any]:
    """Dispatch nightly-e2e.yml at ``ref`` and record the run it starts (or an unmatched dispatch to be timed out)."""
    sha = gh.resolve(ref)
    known = set(state["seen"])
    gh.dispatch(ref, sha)
    entry: dict[str, Any] = {
        "ref": ref,
        "sha": sha,
        "dispatched_at": now,
        "run_id": None,
        "url": None,
    }
    deadline = time.time() + lookup_seconds
    while True:
        # The dispatch API returns no run id: the first not-yet-recorded run created since the call is ours.
        fresh = sorted(
            (r for r in gh.runs_since(now - 5) if r["id"] not in known),
            key=lambda r: r["id"],
        )
        if fresh:
            entry.update(run_id=fresh[0]["id"], url=fresh[0]["html_url"])
            state["seen"] = (state["seen"] + [fresh[0]["id"]])[-200:]
            break
        if time.time() >= deadline:
            break
        sleep(5)
    state["pending"].append(entry)
    return entry


def _card_body(kind: str, entry: dict[str, Any], ids: list[str], summary: str) -> str:
    url = entry.get("url") or "(no run was created)"
    head = f"ref `{entry['ref']}` = `{entry['sha']}`, dispatched {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(entry['dispatched_at']))}"
    if kind == "failed":
        listed = ids[:MAX_LISTED]
        tests = "\n".join(f"- `{t}`" for t in listed)
        if len(ids) > len(listed):
            tests += f"\n- … and {len(ids) - len(listed)} more (see the run log)"
        return (
            f"The nightly e2e (the same `e2e` job the upstream-sync gate runs) is RED.\n\n"
            f"Run: {url}\n{head}\n\n{summary}\n\nFailing tests ({len(ids)}):\n{tests}\n\n"
            "Fix main (or the test) through a PR on daveey/hermes; until e2e is green on main the upstream-sync "
            "gate refuses every sync. Later failing nightlies with these same tests are added to this card as "
            "comments; a green run is commented here too. Filed by scripts/ci/nightly_e2e.py on hermes-hub."
        )
    return (
        f"The nightly e2e did NOT run to completion within {DEADLINE_HOURS:g} h, so main's e2e state is unknown.\n\n"
        f"Run: {url}\n{head}\n\n{summary}\n\nFind why (runner capacity: `hermes-hub` self-hosted runners; "
        "queue; workflow error) and fix it. Filed by scripts/ci/nightly_e2e.py on hermes-hub."
    )


def _file_or_update(
    board: Any, state: dict[str, Any], key: str, title: str, body: str, comment: str
) -> str:
    """Comment on this key's open card, else file a new one. Returns what happened, with the card id."""
    rec = state["cards"].get(key)
    if rec and board.status(rec["id"]) in OPEN:
        board.comment(rec["id"], comment)
        return f"updated {rec['id']} ({key})"
    n = (rec or {}).get("n", 0) + 1
    try:
        card_id = board.create(title, body, f"nightly-e2e:{key}:{n}")
    except CreateRefused as refused:
        card_id = board.create(
            title,
            body,
            f"nightly-e2e:{key}:{n}",
            distinct_from=[str(m.get("id")) for m in refused.matches if m.get("id")],
            distinct_reason="the nightly e2e finding for this exact set of failing tests",
        )
    state["cards"][key] = {"id": card_id, "n": n}
    return f"filed {card_id} ({key})"


def _e2e_job(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    named = [
        j
        for j in jobs
        if j.get("name") == E2E_JOB or str(j.get("name", "")).endswith(f"/ {E2E_JOB}")
    ]
    return named[0] if named else None


def _settle(
    gh: GitHub, board: Any, state: dict[str, Any], entry: dict[str, Any], *, now: float
) -> tuple[bool, str]:
    """(done, message) for one pending dispatch."""
    age = now - entry["dispatched_at"]
    url = entry.get("url")
    if not entry.get("run_id"):
        found = [
            r
            for r in gh.runs_since(entry["dispatched_at"] - 5)
            if r["id"] not in set(state["seen"])
        ]
        if found:
            first = sorted(found, key=lambda r: r["id"])[0]
            entry.update(run_id=first["id"], url=first["html_url"])
            state["seen"] = (state["seen"] + [first["id"]])[-200:]
            url = entry["url"]
        elif age > RUN_LOOKUP_MINUTES * 60:
            return True, "did-not-run: " + _file_or_update(
                board,
                state,
                "did-not-run",
                "Nightly e2e did not run (no workflow run was created)",
                _card_body(
                    "did-not-run", entry, [], "GitHub created no run for the dispatch."
                ),
                f"Nightly e2e did not run again: no run was created for the dispatch of `{entry['sha']}` "
                f"at {time.strftime('%H:%M UTC', time.gmtime(entry['dispatched_at']))}.",
            )
        else:
            return False, "waiting for the run to appear"
    run = gh.run(entry["run_id"])
    if run.get("status") != "completed":
        if age <= DEADLINE_HOURS * 3600:
            return False, f"run {entry['run_id']} {run.get('status')}"
        return True, "did-not-run: " + _file_or_update(
            board,
            state,
            "did-not-run",
            "Nightly e2e did not finish in time",
            _card_body(
                "did-not-run",
                entry,
                [],
                f"Run status after {age / 3600:.1f} h: {run.get('status')}.",
            ),
            f"Nightly e2e did not finish in {DEADLINE_HOURS:g} h again: {url} is still {run.get('status')}.",
        )
    conclusion = run.get("conclusion")
    job = _e2e_job(gh.jobs(entry["run_id"]))
    job_conclusion = (job or {}).get("conclusion") or conclusion
    if job_conclusion == "success":
        notes = []
        for key, rec in sorted(state["cards"].items()):
            if board.status(rec["id"]) in OPEN:
                board.comment(
                    rec["id"],
                    f"Nightly e2e is GREEN: {url} (`{entry['ref']}` = `{entry['sha']}`). "
                    "Close this card if the cause is fixed.",
                )
                notes.append(rec["id"])
        return True, "green" + (f"; commented on {', '.join(notes)}" if notes else "")
    if job_conclusion == "failure":
        log = gh.job_log(job["id"]) if job else ""
        ids = failing_tests(log)
        summary_m = SUMMARY_LINE.findall(log)
        summary = summary_m[-1] if summary_m else "(no pytest summary line in the log)"
        if not ids:
            ids = [
                "(no failing test id parsed: the e2e job failed outside pytest, see the run)"
            ]
        key = failure_key(ids)
        return True, "failed: " + _file_or_update(
            board,
            state,
            key,
            f"Nightly e2e RED on main: {len(ids)} failing test(s), first {ids[0].split('::')[0]}",
            _card_body("failed", entry, ids, summary),
            f"Nightly e2e failed again with the same {len(ids)} test(s): {url} "
            f"(`{entry['ref']}` = `{entry['sha']}`).\n{summary}",
        )
    return True, f"{job_conclusion or conclusion}: " + _file_or_update(
        board,
        state,
        "did-not-run",
        "Nightly e2e did not run to completion",
        _card_body(
            "did-not-run",
            entry,
            [],
            f"Run conclusion: {conclusion}; e2e job: {job_conclusion}.",
        ),
        f"Nightly e2e did not complete again ({job_conclusion or conclusion}): {url}.",
    )


def _settle_error(
    board: Any, state: dict[str, Any], entry: dict[str, Any], err: Exception, *, now: float
) -> tuple[bool, str]:
    """Within the deadline: stay pending, the next tick retries. Past it: the "did not run" card carries the error;
    if the board fails too, stay pending and say so (``BOARD_FAILED``): a run is never dropped without a card."""
    age = now - entry["dispatched_at"]
    if age <= DEADLINE_HOURS * 3600:
        return False, f"error (retrying): {err!r}"
    try:
        return True, "did-not-run after errors: " + _file_or_update(
            board,
            state,
            "did-not-run",
            "Nightly e2e result unknown (settling the run kept failing)",
            _card_body(
                "did-not-run",
                entry,
                [],
                f"Settling the run still failed {age / 3600:.1f} h after dispatch: {err!r}",
            ),
            f"Nightly e2e result unknown again: settling {entry.get('url') or entry['sha']} still failed after "
            f"{DEADLINE_HOURS:g} h: {err!r}",
        )
    except SETTLE_ERRORS as board_err:
        return False, f"{BOARD_FAILED}, kept pending: settle error {err!r}; filing the card: {board_err!r}"


def report(gh: GitHub, board: Any, state: dict[str, Any], *, now: float) -> list[str]:
    out: list[str] = []
    still: list[dict[str, Any]] = []
    for entry in list(state["pending"]):
        try:
            done, msg = _settle(gh, board, state, entry, now=now)
        except SETTLE_ERRORS as err:
            done, msg = _settle_error(board, state, entry, err, now=now)
        out.append(f"{entry.get('url') or entry['sha']}: {msg}")
        if not done:
            still.append(entry)
    state["pending"] = still
    return out


def _token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise SystemExit("nightly_e2e: GH_TOKEN is required")
    return tok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", choices=["dispatch", "report"])
    ap.add_argument(
        "--ref", default="main", help="dispatch: branch or SHA to test (default main)"
    )
    ap.add_argument("--repo", default=REPO)
    ap.add_argument(
        "--workflow-ref",
        default="main",
        help="dispatch: the ref whose nightly-e2e.yml runs (default main)",
    )
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--hermes", default="hermes")
    args = ap.parse_args(argv)
    gh = RestGitHub(_token(), args.repo, args.workflow_ref)
    with locked_state(args.state) as state:
        now = time.time()  # after the lock: a dispatch that waited on a report tick records when it really ran
        if args.command == "dispatch":
            entry = dispatch(gh, state, args.ref, now=now)
            print(
                f"nightly-e2e: dispatched {entry['ref']} = {entry['sha']}: {entry['url'] or 'run not visible yet'}"
            )
            return 0
        lines = report(gh, NightlyBoard(args.hermes), state, now=now)
    if lines:  # silent when nothing is pending: a no-agent cron job with empty stdout stays quiet
        print("nightly-e2e:\n" + "\n".join(lines))
    return 1 if any(BOARD_FAILED in line for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
