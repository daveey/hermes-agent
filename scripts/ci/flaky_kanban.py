#!/usr/bin/env python3
"""File ONE Kanban deflake card per flaky test id (t_94f2c8ba).

scripts/run_tests_parallel.py retries a failed test once in the same job. A
test that passes only on that retry keeps the job green but must never pass
silently: the runner writes it to ``--flaky-report`` JSON, the CI step
``flaky_kanban.py --annotate <report>`` turns each entry into a warning
annotation titled ``FLAKY (passed only on retry)`` (first message line = test
id, then the failure tail), and the hub cron reads those annotations back and
files one card per test id.

* first flake of a test id: create a card (idempotency key ``deflake:<id>``)
* later flakes while that card is open: append a comment to it
* flake after its card was closed (done/archived): open a new card that links
  the old one, since the fix did not hold

Modes:

  flaky_kanban.py --annotate flaky.json               # CI step: emit annotations
  flaky_kanban.py --report flaky.json [--report ...] --run-url URL [--branch B]
  flaky_kanban.py --from-github --repo OWNER/NAME     # hub cron: poll annotations

``--from-github`` reads ``GH_TOKEN``/``GITHUB_TOKEN``, walks recently completed
workflow runs, and remembers processed run attempts in ``--state`` so each run
is filed once. Cards go through the ``hermes kanban`` CLI of the host it runs
on (the hub's board).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Protocol

ANNOTATION_TITLE = "FLAKY (passed only on retry)"
CLOSED = {"done", "archived"}
DEFAULT_STATE = Path(os.path.expanduser("~/.hermes/state/ci-flaky-cards.json"))
EXCERPT_LIMIT = 3000


class Board(Protocol):
    def create(self, title: str, body: str, key: str) -> str: ...
    def status(self, card_id: str) -> str | None: ...
    def comment(self, card_id: str, body: str) -> None: ...


class CliBoard:
    """The host's Kanban board through ``hermes kanban`` (never its DB directly)."""

    def __init__(self, hermes: str | list[str] = "hermes", assignee: str = "default") -> None:
        self.hermes = [hermes] if isinstance(hermes, str) else list(hermes)
        self.assignee = assignee

    def _run(self, *args: str, stdin: str | None = None) -> str:
        proc = subprocess.run([*self.hermes, "kanban", *args], input=stdin, capture_output=True,
                              text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"hermes kanban {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
        return proc.stdout

    def create(self, title: str, body: str, key: str) -> str:
        out = self._run("create", title, "--assignee", self.assignee, "--idempotency-key", key,
                        "--created-by", "ci-flaky-cards", "--body-file", "-", "--json", stdin=body)
        data = json.loads(out)
        return str(data.get("id") or data.get("task_id") or data["task"]["id"])

    def status(self, card_id: str) -> str | None:
        try:
            data = json.loads(self._run("show", card_id, "--json"))
        except (RuntimeError, ValueError):
            return None
        task = data.get("task", data)
        return task.get("status")

    def comment(self, card_id: str, body: str) -> None:
        self._run("comment", card_id, body)


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("cards", {})       # test id -> {"card": id, "generation": n}
    state.setdefault("runs", [])        # processed "<run id>/<attempt>"
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["runs"] = state["runs"][-1000:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _card_body(entry: dict[str, str], run_url: str, branch: str, previous: str | None) -> str:
    excerpt = (entry.get("first_attempt_excerpt") or "")[-EXCERPT_LIMIT:]
    lines = [
        f"Flaky test: `{entry['test_id']}`",
        "",
        f"It failed on its first attempt and passed when CI retried it ({run_url}, branch `{branch}`).",
        "The retry keeps the job green, so this card is the only signal. Fix the cause, don't widen the retry.",
        "",
        "Usual cause on the hub runners: a fixed wall-clock deadline (sleep, short wait/join timeout, short",
        "lease) that a CPU-throttled container misses. Wait on events or use generous timeouts that return",
        "as soon as the condition holds. Reproduce by starving the process (SIGSTOP/SIGCONT duty cycle) rather",
        "than on a quiet laptop.",
    ]
    if previous:
        lines += ["", f"An earlier deflake card {previous} was closed, but the test flaked again."]
    if entry.get("retried") == "file":
        lines += ["", "The failure could not be pinned to test ids (collection error or crash), so the whole file was retried."]
    lines += ["", "First-attempt output (tail):", "```", excerpt, "```"]
    return "\n".join(lines)


def file_cards(entries: Iterable[dict[str, str]], *, run_url: str, branch: str, board: Board,
               state: dict[str, Any]) -> list[str]:
    """Open or append one card per distinct test id; returns one log line per id."""
    log = []
    seen: set[str] = set()
    for entry in entries:
        test_id = entry["test_id"]
        if test_id in seen:
            continue
        seen.add(test_id)
        known = state["cards"].get(test_id)
        if known and board.status(known["card"]) not in CLOSED | {None}:
            board.comment(known["card"], f"Flaked again: {run_url} (branch `{branch}`).\n\n```\n"
                          f"{(entry.get('first_attempt_excerpt') or '')[-1500:]}\n```")
            log.append(f"appended {known['card']} {test_id}")
            continue
        generation = (known or {}).get("generation", 0) + (1 if known else 0)
        key = f"deflake:{test_id}" + (f"#{generation}" if generation else "")
        card = board.create(f"Deflake {test_id}", _card_body(entry, run_url, branch, (known or {}).get("card")), key)
        state["cards"][test_id] = {"card": card, "generation": generation}
        log.append(f"opened {card} {test_id}")
    return log


def read_report(raw: bytes | str) -> list[dict[str, str]]:
    data = json.loads(raw)
    if data.get("version") != 1:
        raise ValueError(f"unknown flaky report version: {data.get('version')!r}")
    return list(data.get("flaky") or [])


# --- CI side: report -> annotations --------------------------------------------


def _escape(text: str) -> str:
    """Workflow-command escaping for an annotation message."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotations(entries: Iterable[dict[str, str]], excerpt_limit: int = 1500) -> list[str]:
    lines = []
    for e in entries:
        tail = (e.get("first_attempt_excerpt") or "").strip()[-excerpt_limit:]
        msg = e["test_id"] + ("\n" + tail if tail else "")
        lines.append(f"::warning title={ANNOTATION_TITLE}::{_escape(msg)}")
    return lines


def parse_annotation(message: str) -> dict[str, str]:
    test_id, _, excerpt = message.partition("\n")
    return {"test_id": test_id.strip(), "first_attempt_excerpt": excerpt, "retried": "tests" if "::" in test_id else "file"}


# --- Hub side: annotations -> cards (cron) ---------------------------------------


def _gh(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-ci-flaky-cards"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def poll_github(repo: str, token: str, board: Board, state: dict[str, Any], *, runs: int = 40) -> list[str]:
    """File cards for FLAKY annotations on recently completed runs not seen before."""
    api = f"https://api.github.com/repos/{repo}"
    log: list[str] = []
    done = set(state["runs"])
    page = _gh(f"{api}/actions/runs?status=completed&per_page={runs}", token)
    for run in sorted(page.get("workflow_runs", []), key=lambda r: r["id"]):
        key = f"{run['id']}/{run.get('run_attempt', 1)}"
        if key in done:
            continue
        jobs = _gh(f"{api}/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100", token)
        for job in jobs.get("jobs", []):
            if "Run tests" not in job.get("name", ""):
                continue
            entries = [parse_annotation(a.get("message", ""))
                       for a in _gh(f"{api}/check-runs/{job['id']}/annotations?per_page=100", token)
                       if a.get("title") == ANNOTATION_TITLE]
            if entries:
                log += file_cards(entries, run_url=job.get("html_url") or run["html_url"],
                                  branch=run.get("head_branch") or "?", board=board, state=state)
        state["runs"].append(key)
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--annotate", metavar="REPORT", help="CI: print one FLAKY annotation per report entry")
    ap.add_argument("--report", action="append", default=[], help="flaky report JSON (repeatable)")
    ap.add_argument("--run-url", default="(local run)")
    ap.add_argument("--branch", default="?")
    ap.add_argument("--from-github", action="store_true")
    ap.add_argument("--repo", default="daveey/hermes")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--hermes", default=os.environ.get("HERMES_BIN", "hermes"))
    ap.add_argument("--assignee", default="default")
    args = ap.parse_args(argv)

    if args.annotate:
        path = Path(args.annotate)
        if not path.is_file():
            print(f"flaky_kanban: no flaky report at {path} (the test step did not get that far)")
            return 0
        entries = read_report(path.read_text(encoding="utf-8"))
        for line in annotations(entries):
            print(line)
        print(f"flaky_kanban: {len(entries)} flaky test id(s)")
        return 0

    board = CliBoard(args.hermes, args.assignee)
    state = load_state(args.state)
    if args.from_github:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("flaky_kanban: GH_TOKEN/GITHUB_TOKEN not set", file=sys.stderr)
            return 2
        log = poll_github(args.repo, token, board, state)
    else:
        entries: list[dict[str, str]] = []
        for path in args.report:
            entries += read_report(Path(path).read_text(encoding="utf-8"))
        log = file_cards(entries, run_url=args.run_url, branch=args.branch, board=board, state=state)
    save_state(args.state, state)
    for line in log:  # silent when nothing flaked (no-agent cron delivers stdout)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
