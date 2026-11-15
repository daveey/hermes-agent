#!/usr/bin/env python3
"""Nightly e2e on main: dispatch .github/workflows/nightly-e2e.yml and report the result on the Kanban board.

The fork's e2e suite sat red on main for days because no workflow ran it on main and nothing watched it; the
upstream-sync gate then refused every sync for a cause that was not upstream's. This is the watcher. It runs on
hermes-hub from two Hermes cron jobs (never from a laptop, never a GitHub ``schedule:``):

  nightly_e2e.py dispatch [--ref main]   02:00 PT: resolve the ref to a SHA, dispatch nightly-e2e.yml, record the run
  nightly_e2e.py report                  every 10 min: settle each recorded run, file/update/comment the board

Reporting (``report``), always within ``DEADLINE_HOURS`` of the dispatch:

* failed      -> ONE open red card per alert scope (below). A later failing run of that scope comments on it with
                 the run link and the newly failing / no longer failing / still failing test IDs against the last
                 run it reported, whatever the overlap (subset, superset, disjoint). Once that card is done or
                 archived, the next red run files the next generation. Body: run link + tests.
* not finished in time / cancelled / never started -> ONE "did not run" card, later occurrences comment on it.
* green while a card of ours is open -> comment the green run on every open one.
* GitHub (or the board) keeps erroring -> retried every tick; past the deadline the run gets the "did not run" card
                 with the error. If the board itself fails, the run stays pending and ``report`` exits 1, so the cron
                 run shows the error; a run is never dropped without a card.

Alert intent: titles name the tested ref ("RED on <ref>"). Cards of a non-main ref, and of any run given
``--acceptance-marker LABEL --acceptance-owner-task t_...`` (dispatch, or report to label already-pending runs),
say "acceptance fixture; do not repair main" / "not a current main incident" and request no repair; the owner is
recorded as creator_task_id so the owner's cleanup can archive them. Each intent (main, ref, acceptance marker)
keeps its own cards: production main alerts are unchanged (default assignee, "Fix main" instruction).

The fingerprint version filed a card per exact failing set, so one incident whose failures shifted kept opening new
red cards. Its state (``failed-<sha1>`` records, no IDs) migrates on the next red run of each scope: an open legacy
card is reused (other open ones of the scope get one "superseded" comment), its baseline is the fingerprint's set if
the run hashes to it or the body's test list if that hashes to it, else it is reported as unknown.

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
from flaky_kanban import CliBoard, CreateRefused

REPO = "softmax-universe/daveey-hermes"
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
        creator_task_id: str | None = None,
    ) -> str:
        override: list[str] = []
        for card_id in distinct_from or []:
            override += ["--distinct-from", card_id]
        if override:
            override += [
                "--distinct-reason",
                distinct_reason or "a separate nightly e2e finding",
            ]
        # `hermes kanban create` records creator_task_id from HERMES_KANBAN_TASK (the supported attribution path).
        # Only an explicit acceptance run sets it; production cards keep creator_task_id None.
        # The CLI drops a TASK identity that has no live RUN_ID, so pair them from the owner's live run.
        extra_env = None
        if creator_task_id:
            shown = self._show(creator_task_id)
            if shown is None:
                raise RuntimeError(f"acceptance owner {creator_task_id} could not be read (kanban show failed)")
            live = [
                r for r in (shown.get("runs") or [])
                if r.get("status") == "running" and not r.get("ended_at")
            ]
            if not live:
                raise RuntimeError(f"acceptance owner {creator_task_id} has no live run to attribute cards to")
            # The CLI accepts only the task's current run; take it from the same payload as the run list.
            current = (shown.get("task") or {}).get("current_run_id")
            if current is not None:
                match = [r for r in live if str(r.get("id")) == str(current)]
                if not match:
                    raise RuntimeError(
                        f"acceptance owner {creator_task_id} current run {current} is not a live run"
                    )
                live = match
            elif len(live) > 1:
                raise RuntimeError(
                    f"acceptance owner {creator_task_id} has {len(live)} running runs and no current_run_id"
                )
            extra_env = {"HERMES_KANBAN_TASK": creator_task_id, "HERMES_KANBAN_RUN_ID": str(live[-1]["id"])}
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
            # A live-run identity makes this an agent-run create, which must declare needs; a fixture needs none.
            *(["--no-requires"] if creator_task_id else []),
            stdin=body,
            extra_env=extra_env,
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
    acceptance: tuple[str, str] | None = None,
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
    if acceptance:
        entry["acceptance_marker"], entry["acceptance_owner"] = acceptance
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


def scope_of(entry: dict[str, Any]) -> str:
    """Alert intent: ``main`` (production), ``ref:<ref>`` (a historical/other ref) or ``acceptance:<marker>``.

    Only production ``main`` cards are repair work. Each scope has its own cards, so a fixture or historical-ref
    run never comments on, or is deduped into, a production card. Within a scope there is one open red card.
    """
    if entry.get("acceptance_marker"):
        return f"acceptance:{entry['acceptance_marker']}"
    return "main" if entry["ref"] == "main" else f"ref:{entry['ref']}"


def _scoped_key(entry: dict[str, Any], key: str) -> str:
    scope = scope_of(entry)
    return key if scope == "main" else f"{scope}|{key}"


def _title(entry: dict[str, Any], failed_ids: list[str] | None, base: str) -> str:
    tag = f" ACCEPTANCE[{entry['acceptance_marker']}]" if entry.get("acceptance_marker") else ""
    if failed_ids is not None:
        return (
            f"Nightly e2e{tag} RED on {entry['ref']}: {len(failed_ids)} failing test(s), "
            f"first {failed_ids[0].split('::')[0]}"
        )
    return f"Nightly e2e{tag} {base} (ref {entry['ref']})"


def _scope_note(entry: dict[str, Any]) -> str:
    if entry.get("acceptance_marker"):
        return (
            f"ACCEPTANCE [{entry['acceptance_marker']}]: acceptance fixture; do not repair main. "
            "This alert exists to test the nightly reporter, not to report a production incident."
        )
    if entry["ref"] != "main":
        return (
            f"Tested ref `{entry['ref']}` is not `main`: this result does not establish a current main incident. "
            "Do not repair main for it."
        )
    return ""


def _card_body(kind: str, entry: dict[str, Any], ids: list[str], summary: str) -> str:
    note = _scope_note(entry)
    prefix = note + "\n\n" if note else ""
    return prefix + _card_body_main(kind, entry, ids, summary, production=not note)


def _card_body_main(
    kind: str, entry: dict[str, Any], ids: list[str], summary: str, *, production: bool
) -> str:
    url = entry.get("url") or "(no run was created)"
    head = f"ref `{entry['ref']}` = `{entry['sha']}`, dispatched {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(entry['dispatched_at']))}"
    if kind == "failed":
        listed = ids[:MAX_LISTED]
        tests = "\n".join(f"- `{t}`" for t in listed)
        if len(ids) > len(listed):
            tests += f"\n- … and {len(ids) - len(listed)} more (see the run log)"
        action = (
            "Fix main (or the test) through a PR on daveey/hermes; until e2e is green on main the upstream-sync "
            "gate refuses every sync. "
            if production
            else "No repair is requested by this card. "
        )
        return (
            f"The nightly e2e (the same `e2e` job the upstream-sync gate runs) is RED at `{entry['ref']}`.\n\n"
            f"Run: {url}\n{head}\n\n{summary}\n\nFailing tests ({len(ids)}):\n{tests}\n\n"
            f"{action}Later failing nightlies of the same scope are added to this card as comments listing newly "
            "failing, no longer failing and still failing tests (one open red card per scope; a red run after this "
            "card is done or archived files a new one); a green run is commented here too. "
            "Filed by scripts/ci/nightly_e2e.py on hermes-hub."
        )
    if not production:
        return (
            f"The nightly e2e did NOT run to completion within {DEADLINE_HOURS:g} h for ref `{entry['ref']}`.\n\n"
            f"Run: {url}\n{head}\n\n{summary}\n\nNo repair is requested by this card. "
            "Filed by scripts/ci/nightly_e2e.py on hermes-hub."
        )
    return (
        f"The nightly e2e did NOT run to completion within {DEADLINE_HOURS:g} h, so main's e2e state is unknown.\n\n"
        f"Run: {url}\n{head}\n\n{summary}\n\nFind why (runner capacity: `hermes-hub` self-hosted runners; "
        "queue; workflow error) and fix it. Filed by scripts/ci/nightly_e2e.py on hermes-hub."
    )


def _is_open(board: Any, card_id: str) -> bool:
    status = board.status(card_id)
    if status is None:
        raise RuntimeError(f"nightly card {card_id} status is unreadable; refusing to replace its owner")
    return status not in {"done", "archived"}


def _create(
    board: Any, entry: dict[str, Any], key: str, n: int, title: str, body: str, reason: str
) -> str:
    owner = entry.get("acceptance_owner")
    extra = {"creator_task_id": owner} if owner else {}
    try:
        return board.create(title, body, f"nightly-e2e:{key}:{n}", **extra)
    except CreateRefused as refused:
        return board.create(
            title,
            body,
            f"nightly-e2e:{key}:{n}",
            distinct_from=[str(m.get("id")) for m in refused.matches if m.get("id")],
            distinct_reason=reason,
            **extra,
        )


def _noted(entry: dict[str, Any], text: str) -> str:
    note = _scope_note(entry)
    return f"{note}\n{text}" if note else text


def _file_or_update(
    board: Any,
    state: dict[str, Any],
    entry: dict[str, Any],
    key: str,
    title: str,
    body: str,
    comment: str,
) -> str:
    """Comment on this scope+key's open card, else file a new one. Returns what happened, with the card id."""
    key = _scoped_key(entry, key)
    rec = state["cards"].get(key)
    if rec and _is_open(board, rec["id"]):
        board.comment(rec["id"], _noted(entry, comment))
        return f"updated {rec['id']} ({key})"
    n = (rec or {}).get("n", 0) + 1
    card_id = _create(board, entry, key, n, title, body, "the nightly e2e finding for this alert scope")
    state["cards"][key] = {"id": card_id, "n": n, "scope": scope_of(entry)}
    return f"filed {card_id} ({key})"


# --- red cards: one open card per alert scope -------------------------------------------------------------------
#
# State record under _scoped_key(entry, RED_KEY): {"id", "n" (generation), "scope", "failing": the failing IDs the
# card's last comment (or body) reported, None when unknown}. "failing" is written only after that comment lands, so
# a failed comment is retried next tick against the same baseline. Records the fingerprint version wrote
# ("failed-<sha1 of the sorted IDs>", one card per exact set, no IDs) are migrated by the next red run of their scope.

RED_KEY = "red"
LEGACY_RED_KEY = re.compile(r"(?:^|\|)failed-[0-9a-f]{12}$")
BODY_IDS = re.compile(r"^Failing tests \((\d+)\):\n((?:- `[^`\n]+`(?:\n|$))+)", re.MULTILINE)
LISTED_ID = re.compile(r"^- `([^`\n]+)`$", re.MULTILINE)


def _rec_scope(key: str, rec: dict[str, Any]) -> str:
    # Records filed before scopes existed carry no "scope" and only ever named main.
    return rec.get("scope") or (key.rsplit("|", 1)[0] if "|" in key else "main")


def _legacy_red(state: dict[str, Any], scope: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (k, r)
        for k, r in sorted(state["cards"].items())
        if LEGACY_RED_KEY.search(k) and _rec_scope(k, r) == scope
    ]


def _legacy_baseline(board: Any, key: str, rec: dict[str, Any], ids: list[str]) -> list[str] | None:
    """The failing IDs a legacy card stands for: its fingerprint when the current run hashes to it, else the
    body's full test list if that list hashes to the fingerprint (a truncated or edited list does not); else None."""
    fingerprint = key.rsplit("|", 1)[-1]
    if failure_key(ids) == fingerprint:
        return sorted(ids)
    m = BODY_IDS.search((board.card(rec["id"]) or {}).get("body") or "")
    if m:
        listed = LISTED_ID.findall(m.group(2))
        if len(listed) == int(m.group(1)) and failure_key(listed) == fingerprint:
            return sorted(listed)
    return None


def _id_section(name: str, ids: list[str]) -> str:
    listed = "\n".join(f"- `{t}`" for t in ids[:MAX_LISTED]) or "- none"
    if len(ids) > MAX_LISTED:
        listed += f"\n- … and {len(ids) - MAX_LISTED} more (see the run log)"
    return f"{name} ({len(ids)}):\n{listed}"


def _red_comment(entry: dict[str, Any], before: list[str] | None, ids: list[str], summary: str) -> str:
    head = f"Nightly e2e is RED again: {entry.get('url')} (`{entry['ref']}` = `{entry['sha']}`).\n{summary}\n\n"
    if before is None:
        return _noted(entry, head + (
            "Baseline unknown: this card was filed before the reporter recorded failing test IDs, and its body "
            "could not be matched to its fingerprint, so this run cannot be split into newly / no longer / still "
            "failing. Later runs are compared against this one.\n\n" + _id_section("Failing now", ids)
        ))
    now, prev = set(ids), set(before)
    sections = [
        _id_section("Newly failing", sorted(now - prev)),
        _id_section("No longer failing", sorted(prev - now)),
        _id_section("Still failing", sorted(now & prev)),
    ]
    return _noted(entry, head + "\n\n".join(sections))


def _adopt_legacy(
    board: Any, state: dict[str, Any], entry: dict[str, Any], ids: list[str]
) -> tuple[str, list[str] | None, str] | None:
    """(card id, baseline, creation key) of the open fingerprint-era card this scope keeps, or None.

    The card whose fingerprint is the current set wins, else the first by key. Every other open one is told where
    results now go and dropped from state before the update is posted, so a retry never re-posts that notice."""
    open_legacy = [(k, r) for k, r in _legacy_red(state, scope_of(entry)) if _is_open(board, r["id"])]
    if not open_legacy:
        return None
    fingerprint = failure_key(ids)
    open_legacy.sort(key=lambda kr: kr[0].rsplit("|", 1)[-1] != fingerprint)  # stable: key order otherwise
    (key, kept), *duplicates = open_legacy
    for dup_key, dup in duplicates:
        board.comment(dup["id"], _noted(entry, (
            f"Superseded by {kept['id']}: the nightly e2e now keeps one red card per alert scope, and later results "
            "for this scope are posted there. This duplicate can be closed."
        )))
        del state["cards"][dup_key]
    return kept["id"], _legacy_baseline(board, key, kept, ids), f"nightly-e2e:{key}:{kept['n']}"


def _report_red(
    board: Any, state: dict[str, Any], entry: dict[str, Any], ids: list[str], summary: str
) -> str:
    """Comment the scope's open red card with the delta to its last failing set, else file the next generation."""
    key = _scoped_key(entry, RED_KEY)
    rec = state["cards"].get(key)
    if rec and _is_open(board, rec["id"]):
        target: tuple[str, list[str] | None, str] | None = (
            rec["id"], rec.get("failing"), rec["idempotency_key"]
        )
    else:
        target = _adopt_legacy(board, state, entry, ids)
    n = (rec or {}).get("n", 0)
    if target is None:
        n += 1
        card_id = _create(
            board, entry, key, n, _title(entry, ids, ""), _card_body("failed", entry, ids, summary),
            "the nightly e2e red card of this alert scope (the previous one is closed)",
        )
        done = f"filed {card_id} ({key})"
        creation_key = f"nightly-e2e:{key}:{n}"
    else:
        card_id, before, creation_key = target
        board.comment(card_id, _red_comment(entry, before, ids, summary))
        done = f"updated {card_id} ({key})"
    for legacy_key, _ in _legacy_red(state, scope_of(entry)):  # migrated (or closed): never consulted again
        del state["cards"][legacy_key]
    state["cards"][key] = {
        "id": card_id, "n": n, "scope": scope_of(entry), "failing": sorted(ids),
        "idempotency_key": creation_key,
    }
    return done


def _report_green(board: Any, state: dict[str, Any], entry: dict[str, Any]) -> str:
    """Comment the green run once on every open card of the scope; a red card also hears what stopped failing."""
    scope, notes = scope_of(entry), []
    for key, rec in sorted(state["cards"].items()):
        if (
            _rec_scope(key, rec) != scope
            or rec["id"] in notes
            or rec.get("green_run") == entry.get("run_id")  # already told, before a later card's comment failed
            or not _is_open(board, rec["id"])
        ):
            continue
        text = (
            f"Nightly e2e is GREEN: {entry.get('url')} (`{entry['ref']}` = `{entry['sha']}`). "
            "Close this card if the cause is fixed."
        )
        if rec.get("failing"):
            text += "\n\n" + _id_section("No longer failing", rec["failing"])
        board.comment(rec["id"], _noted(entry, text))
        rec["green_run"] = entry.get("run_id")
        if "failing" in rec:
            rec["failing"] = []
        notes.append(rec["id"])
    return "green" + (f"; commented on {', '.join(notes)}" if notes else "")


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
                entry,
                "did-not-run",
                _title(entry, None, "did not run (no workflow run was created)"),
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
            entry,
            "did-not-run",
            _title(entry, None, "did not finish in time"),
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
        return True, _report_green(board, state, entry)
    if job_conclusion == "failure":
        log = gh.job_log(job["id"]) if job else ""
        ids = failing_tests(log)
        summary_m = SUMMARY_LINE.findall(log)
        summary = summary_m[-1] if summary_m else "(no pytest summary line in the log)"
        if not ids:
            ids = [
                "(no failing test id parsed: the e2e job failed outside pytest, see the run)"
            ]
        return True, "failed: " + _report_red(board, state, entry, ids, summary)
    return True, f"{job_conclusion or conclusion}: " + _file_or_update(
        board,
        state,
        entry,
        "did-not-run",
        _title(entry, None, "did not run to completion"),
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
            entry,
            "did-not-run",
            _title(entry, None, "result unknown (settling the run kept failing)"),
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


DEFAULT_CORE = Path.home() / ".hermes" / "hermes-agent"
PIN_FILES = ("nightly_e2e.py", "flaky_kanban.py", "flaky_intake.py", "flaky_http.py",
             "flaky_artifact.py", "flaky_artifact_poll.py")


def _pin_git(core: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(core), *args], capture_output=True, check=True, timeout=15
    ).stdout


def check_pin(board: Any, state: dict[str, Any], *, core: Path, now: float) -> str | None:
    """Check immutable exports, not an in-checkout reporter. Never fetch in a cron tick."""
    running = Path(__file__).resolve()
    core = core.expanduser().resolve()
    if running.is_relative_to(core):
        return None
    # Freeze the ref so an updater fetch cannot mix content, blob IDs and dates from different heads.
    main = _pin_git(core, "rev-parse", "origin/main").decode().strip()
    blobs, stale = [], []
    for name in PIN_FILES:
        path = f"scripts/ci/{name}"
        content = _pin_git(core, "show", f"{main}:{path}")
        blobs.append(_pin_git(core, "rev-parse", f"{main}:{path}").decode().strip())
        deployed = running.parent / name
        if not deployed.is_file() or hashlib.sha256(deployed.read_bytes()).digest() != hashlib.sha256(content).digest():
            changed = int(_pin_git(core, "log", "-1", "--format=%ct", main, "--", path))
            if now - changed > 2 * 3600:
                stale.append(name)
    if not stale:
        return None
    key = "nightly-e2e:stale-pin:" + ":".join(blobs)
    body = (
        f"Nightly reporter pin `{running.parent.name}` lags main `{main}`. "
        f"Files changed more than 2 h ago: {', '.join(stale)}.\n\n"
        f"On hermes-hub, export ALL sibling scripts ({', '.join(PIN_FILES)}) "
        f"from `{main}` in `{core}` to `~/.hermes/lib/nightly-e2e/{main[:12]}/` "
        "using git show <sha>:scripts/ci/<file>. Point SCRIPT in BOTH "
        "`~/.hermes/scripts/nightly_e2e_dispatch.py` and `~/.hermes/scripts/nightly_e2e_report.py` "
        "there. Verify every pinned SHA-256 hash against main and the real report cron is ok and silent. "
        "Use the installed Hermes runtime for hermes_cli (deadline transport and scope heartbeat helpers); "
        "do not export a partial hermes_cli package. Verify those imports under the cron launcher. "
        "Do not move the live core checkout."
    )
    # Separate from run cards: a green nightly must not imply that the deployment drift is repaired.
    pins = state.setdefault("pin_cards", {})
    if key in pins:
        rec = pins[key]
        card_id = rec["id"]
        if rec["body"] != body and _is_open(board, card_id):
            board.comment(card_id, body)
            rec["body"] = body
            return f"stale pin: updated {card_id} (main {main})"
        return None
    try:
        card_id = board.create("Re-pin hermes-hub nightly e2e reporter", body, key)
    except CreateRefused as refused:
        card_id = board.create(
            "Re-pin hermes-hub nightly e2e reporter", body, key,
            distinct_from=[str(m["id"]) for m in refused.matches if m.get("id")],
            distinct_reason="deployment drift for these main blobs, not a nightly test failure",
        )
    pins[key] = {"id": card_id, "body": body}
    return f"stale pin: filed {card_id} (main {main})"


def report(
    gh: GitHub,
    board: Any,
    state: dict[str, Any],
    *,
    now: float,
    acceptance: tuple[str, str] | None = None,
    core: Path | None = None,
) -> list[str]:
    out: list[str] = []
    try:
        note = check_pin(board, state, core=core or DEFAULT_CORE, now=now)
        if note:
            out.append(note)
    except Exception as err:  # health: allow BLE001 -- advisory failures must log one line and never skip settlement
        # This advisory must never prevent pending runs from settling, even for a bug in the check itself.
        out.append(f"pin check failed (continuing): {err!r}")
    still: list[dict[str, Any]] = []
    for entry in list(state["pending"]):
        # Label runs recorded before the flag existed, but never a production-main run: that incident stays real.
        if acceptance and not entry.get("acceptance_marker") and entry.get("ref") != "main":
            entry["acceptance_marker"], entry["acceptance_owner"] = acceptance
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


def _acceptance(ap: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, str] | None:
    marker, owner = args.acceptance_marker, args.acceptance_owner_task
    if not marker and not owner:
        return None
    if not (marker and owner):
        ap.error("--acceptance-marker and --acceptance-owner-task must be given together")
    if not re.fullmatch(r"[\w.-]{1,40}", marker) or not re.fullmatch(r"t_[0-9a-f]{8,}", owner):
        ap.error("invalid --acceptance-marker (word chars, .- only) or --acceptance-owner-task (t_<hex>)")
    running = os.environ.get("HERMES_KANBAN_TASK")
    if running and running != owner:
        ap.error(f"--acceptance-owner-task must be the running task {running}, not a foreign owner")
    return marker, owner


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
    ap.add_argument("--core", type=Path, default=DEFAULT_CORE, help="core checkout for the pinned reporter freshness check")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--hermes", default="hermes")
    ap.add_argument(
        "--acceptance-marker",
        help="mark every card for these runs as an acceptance fixture (not a production incident); "
        "needs --acceptance-owner-task. report: also labels pending non-main-ref runs dispatched without a marker",
    )
    ap.add_argument(
        "--acceptance-owner-task",
        help="the existing task that owns this acceptance run; recorded as creator_task_id of its cards",
    )
    args = ap.parse_args(argv)
    acceptance = _acceptance(ap, args)
    board = NightlyBoard(args.hermes)
    if acceptance and not board.card(acceptance[1]):
        ap.error(f"--acceptance-owner-task {acceptance[1]} is not an existing task")
    gh = RestGitHub(_token(), args.repo, args.workflow_ref)
    with locked_state(args.state) as state:
        now = time.time()  # after the lock: a dispatch that waited on a report tick records when it really ran
        if args.command == "dispatch":
            entry = dispatch(gh, state, args.ref, now=now, acceptance=acceptance)
            print(
                f"nightly-e2e: dispatched {entry['ref']} = {entry['sha']}: {entry['url'] or 'run not visible yet'}"
            )
            return 0
        lines = report(gh, board, state, now=now, acceptance=acceptance, core=args.core)
    if lines:  # silent when nothing is pending: a no-agent cron job with empty stdout stays quiet
        print("nightly-e2e:\n" + "\n".join(lines))
    return 1 if any(BOARD_FAILED in line for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
