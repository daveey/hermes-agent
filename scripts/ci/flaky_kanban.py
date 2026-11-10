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
* a card anyone filed (any status) already cites the job URL: never open another;
  comment it if open, else record it and skip. An open card about the test id that
  this cron did not file gets the comment too (found via ``hermes kanban list``)
* the core overlap guard refuses the create (create_overlap): a match owns the flake
  only if it is open and about this test id (its title/body names it, or its key is
  ``deflake:<id>``). The comment goes to that owner (through ``hermes-hub request``
  when it lives on another gateway) and is read back before anything logs "appended"
  or records it in state. No qualifying match: create anyway, ``--distinct-from`` the
  matches. A delivery that fails or is not confirmed is logged ``FAILED``, its run is
  left unprocessed (retried on the next poll) and the script exits 1 (t_01a189b2c78e9005)

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
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Protocol

ANNOTATION_TITLE = "FLAKY (passed only on retry)"
CLOSED = {"done", "archived"}
OPEN = {"todo", "ready", "running", "scheduled", "blocked", "review", "triage"}
DEFAULT_STATE = Path(os.path.expanduser("~/.hermes/state/ci-flaky-cards.json"))
EXCERPT_LIMIT = 3000


class CreateRefused(RuntimeError):
    """`hermes kanban create --json` refused (exit 2, the JSON on STDOUT, stderr blank): open cards already look
    like this work (code create_overlap). Not a failure: the matches own the incident."""

    def __init__(self, note: str, matches: list[dict[str, Any]]) -> None:
        super().__init__(note)
        self.matches = matches


class Board(Protocol):
    def create(self, title: str, body: str, key: str, *, distinct_from: list[str] | None = None,
               distinct_reason: str | None = None) -> str: ...
    def status(self, card_id: str) -> str | None: ...
    def comment(self, card_id: str, body: str, board: str | None = None) -> None: ...
    def list_cards(self) -> list[dict[str, Any]]: ...   # every card, any status: id, status, title, body
    def comments(self, card_id: str, board: str | None = None) -> list[str]: ...
    def card(self, card_id: str, board: str | None = None) -> dict[str, Any] | None: ...  # status, title, body


class CliBoard:
    """The host's Kanban board through ``hermes kanban`` (never its DB directly)."""

    def __init__(self, hermes: str | list[str] = "hermes", assignee: str = "default") -> None:
        self.hermes = [hermes] if isinstance(hermes, str) else list(hermes)
        self.assignee = assignee

    def _run(self, *args: str, stdin: str | None = None, board: str | None = None,
             extra_env: dict[str, str] | None = None) -> str:
        scope = ["--board", board] if board else []
        env = {**os.environ, **extra_env} if extra_env else None
        if args[:1] == ("create",) and not (extra_env and "HERMES_KANBAN_TASK" in extra_env):
            # A production create must not inherit a running worker's task as creator_task_id.
            env = {k: v for k, v in (env or os.environ).items() if k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID")}
        proc = subprocess.run([*self.hermes, "kanban", *scope, *args], input=stdin, capture_output=True,
                              text=True, timeout=120, check=False, env=env)
        if proc.returncode == 2 and args[0] == "create":
            try:
                ref = json.loads(proc.stdout)
            except ValueError:
                ref = None
            if isinstance(ref, dict) and ref.get("created") is False and ref.get("code") == "create_overlap":
                raise CreateRefused(str(ref.get("note") or ""), list(ref.get("matches") or []))
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"hermes kanban {args[0]} failed ({proc.returncode}): {detail[:400]}")
        return proc.stdout

    def create(self, title: str, body: str, key: str, *, distinct_from: list[str] | None = None,
               distinct_reason: str | None = None) -> str:
        override: list[str] = []
        for card_id in distinct_from or []:
            override += ["--distinct-from", card_id]
        if override:
            override += ["--distinct-reason", distinct_reason or "a separate flaky test"]
        out = self._run("create", title, "--assignee", self.assignee, "--idempotency-key", key,
                        "--created-by", "ci-flaky-cards", "--body-file", "-", "--json", *override, stdin=body)
        data = json.loads(out)
        return str(data.get("id") or data.get("task_id") or data["task"]["id"])

    def _show(self, card_id: str, board: str | None = None) -> dict[str, Any] | None:
        try:
            data = json.loads(self._run("show", card_id, "--json", board=board))
        except (RuntimeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def card(self, card_id: str, board: str | None = None) -> dict[str, Any] | None:
        data = self._show(card_id, board)
        task = (data or {}).get("task", data)
        return task if isinstance(task, dict) and task.get("id") else None

    def status(self, card_id: str) -> str | None:
        return (self.card(card_id) or {}).get("status")

    def comment(self, card_id: str, body: str, board: str | None = None) -> None:
        self._run("comment", card_id, body, board=board)

    def list_cards(self) -> list[dict[str, Any]]:
        return list(json.loads(self._run("list", "--archived", "--json")))

    def comments(self, card_id: str, board: str | None = None) -> list[str]:
        data = self._show(card_id, board) or {}
        return [str(c.get("body") or "") for c in data.get("comments") or []]


class HubRelay:
    """Cards on OTHER gateways, through hermes-hub: read from its replica (``hub.db``, read-only), commented by
    ``hermes-hub request <gw> tasks <profile>/<board>/<id> comment`` (the owning gateway applies it).

    The request id is derived from the target and text, so a retried run re-reads the same request instead of
    posting a second comment (hermes-hub ignores a repeated id with the same envelope).
    """

    def __init__(self, db: Path | None = None, cli: str = "hermes-hub", wait: float = 90.0, poll: float = 3.0) -> None:
        state = os.environ.get("HERMES_HUB_STATE") or os.path.expanduser("~/.local/state/hermes-hub")
        self.db = Path(db) if db else Path(state) / "hub.db"
        self.cli, self.wait, self.poll = cli, wait, poll

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.db.is_file():
            return []
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def self_gateway(self) -> str | None:
        rows = self._query("SELECT v FROM meta WHERE k = 'gateway'")
        return str(rows[0][0]) if rows and rows[0][0] else None

    def card(self, gw: str, board: str, card_id: str) -> dict[str, Any] | None:
        """The replica's view of ``gw``'s card: canonical ``profile/board/id`` rows win over a legacy
        ``board/id`` row (a stale legacy row still said `running` for a card done for days)."""
        rows = self._query("SELECT key, data FROM replica WHERE coll = 'tasks' AND gw = ? AND (key = ? OR key LIKE ?)",
                           (gw, f"{board}/{card_id}", f"%/{board}/{card_id}"))
        best: dict[str, Any] | None = None
        for key, raw in sorted(rows, key=lambda r: -str(r[0]).count("/")):
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == card_id:
                parts = str(key).split("/")
                data["_route"] = key if len(parts) == 3 else f"default/{key}"
                best = data
                break
        return best

    def comment(self, gw: str, board: str, card_id: str, text: str, marker: list[str]) -> tuple[bool | None, str]:
        """``(True, how)`` delivered and read back, ``(False, why)`` refused, ``(None, why)`` not confirmed yet."""
        target = self.card(gw, board, card_id)
        if target is None:
            return False, f"{gw}/{board}/{card_id} is not in the hermes-hub replica"
        rid = "flaky-" + hashlib.sha256(f"{gw}|{target['_route']}|{text}".encode()).hexdigest()[:32]
        proc = subprocess.run([self.cli, "request", gw, "tasks", target["_route"], "comment", "--arg", f"text={text}",
                               "--id", rid], capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode not in (0, 3):  # 3 = hub unreachable, queued locally and sent on the next sync
            return False, f"hermes-hub request exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        deadline = time.monotonic() + self.wait
        state, result = "local", ""
        while True:
            rows = self._query("SELECT state, result FROM sent WHERE id = ?", (rid,))
            state, result = (rows[0][0], rows[0][1] or "") if rows else ("missing", "")
            if state in ("rejected", "failed"):
                return False, f"request {rid} {state}: {result[:300]}"
            if state == "done":
                seen = self.card(gw, board, card_id) or {}
                if any(all(m in str(c.get("body") or "") for m in marker) for c in seen.get("last_comments") or []):
                    return True, f"comment read back via request {rid}"
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll)
        if state == "done" and f"Comment added to {card_id}" in result:
            # The owner's own `hermes kanban comment` receipt; the replica keeps only the last 3 comments.
            return True, f"owner receipt 'Comment added to {card_id}' via request {rid} (not in the replica's last 3)"
        return None, f"request {rid} still {state} after {self.wait:.0f}s ({result[:120]})"


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
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


DONE_WHEN = "Done when the PR is merged to main and the test passes on main's next CI run."


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
        "",
        DONE_WHEN,
        "Opening a PR is not done. Once the PR is open, stay on the card: kanban_block(kind='dependency'/",
        "'time') or kanban_continue until the PR is merged (merge it yourself once its required checks are",
        "green on the current head), then confirm the test passes on main's next CI run. Do not call",
        "kanban_complete while the PR is open.",
    ]
    if previous:
        lines += ["", f"An earlier deflake card {previous} was closed, but the test flaked again."]
    if entry.get("retried") == "file":
        lines += ["", "The failure could not be pinned to test ids (collection error or crash), so the whole file was retried."]
    lines += ["", "First-attempt output (tail):", "```", excerpt, "```"]
    return "\n".join(lines)


def _mentions(text: str, test_id: str) -> bool:
    """``test_id`` appears as a whole id (``tests/x.py::test_a`` is not in ``...::test_ab``)."""
    return re.search(re.escape(test_id) + r"(?![\w\[])", text) is not None


_GH_JOB_URL = re.compile(r"(https://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+)/job/(\d+)")
_TEST_ID = re.compile(r"tests/[\w./-]+\.py(?:::[\w\[\]./-]*[\w\]])+")  # ends on a word char or ']', not on "."


def _cites_job(text: str, url: str) -> bool:
    """``text`` cites the job at ``url``: the URL itself, or (GitHub) its run URL plus its job id.

    Workers often write "run <run url> ... job 123202653589" instead of the job URL
    (t_01a1858daf93c588 did), and that is the same job.
    """
    if url in text:
        return True
    m = _GH_JOB_URL.fullmatch(url)
    return bool(m) and m.group(1) in text and re.search(rf"(?<!\d){m.group(2)}(?!\d)", text) is not None


def _existing_cards(board: Board, run_url: str, test_id: str) -> tuple[list[dict], list[dict]]:
    """Cards on the whole board (any creator, any status) that relate to this flake.

    Returns ``(citing, open_for_test)``: cards that already cite this job URL for this test
    id (both appear in the title, body or comments; one job can flake several test ids, each
    with its own card), and open cards that mention the test id. Comments are read (one
    ``show`` each) only for cards whose title/body already has one of the two.
    """
    citing, open_for_test = [], []
    url = run_url if run_url.startswith(("http://", "https://")) else None
    for card in board.list_cards():
        # Only a card ABOUT this test (see _is_about) owns it: a report that lists it among others does not.
        if not _is_about(card, test_id):
            continue
        text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
        has_url = bool(url) and _cites_job(text, url)
        if url and not has_url:
            has_url = _cites_job(f"{text}\n" + "\n".join(board.comments(card["id"])), url)
        if has_url:
            citing.append(card)
        elif card.get("status") not in CLOSED:
            open_for_test.append(card)
    return citing, open_for_test


def _is_about(card: dict[str, Any], test_id: str) -> bool:
    """The card is about THIS test: its title or its body's lead paragraph names the test id and no other, or its
    key is ``deflake:<id>[#n]``. A card that only quotes the id further down (a report about this cron listing
    several dropped tests, t_01a189b2c78e9005 itself) is not the card that fixes it."""
    key = str(card.get("idempotency_key") or "")
    if key == f"deflake:{test_id}" or key.startswith(f"deflake:{test_id}#"):
        return True
    lead = str(card.get("body") or "").strip().split("\n\n", 1)[0]
    subject = f"{card.get('title') or ''}\n{lead}"
    others = {t for t in _TEST_ID.findall(subject) if t != test_id and not test_id.startswith(t)}
    return _mentions(subject, test_id) and not others


def file_cards(entries: Iterable[dict[str, str]], *, run_url: str, branch: str, board: Board,
               state: dict[str, Any], relay: HubRelay | None = None) -> list[str]:
    """Open or append one card per distinct test id; returns one log line per id.

    Before opening a card it checks the whole board, since other workers (CI watch) file
    cards for the same failures without touching this cron's state: a card that already
    cites this job URL for this test id owns the incident (comment it when open; when closed, skip and record
    it so a LATER job's flake opens a linked new generation), and an open card about the test
    id gets the comment instead of a duplicate. "appended" is logged only after the comment is
    read back; a line starting ``FAILED`` means the flake was not filed (the caller retries the run).
    """
    log = []
    seen: set[str] = set()
    self_gw = relay.self_gateway() if relay else None
    for entry in entries:
        test_id = entry["test_id"]
        if test_id in seen:
            continue
        seen.add(test_id)
        ctx = _Flake(test_id, run_url, branch, entry.get("first_attempt_excerpt") or "", board, state, relay, self_gw)
        known = state["cards"].get(test_id)
        recorded_generation = (known or {}).get("generation", 0)
        owner = ctx.resolve(known) if known else None
        if owner is not None and owner.get("status") not in CLOSED | {None}:
            log.append(_append(ctx, owner, recorded_generation))
            continue
        citing, open_for_test = _existing_cards(board, run_url, test_id)
        open_citing = [c for c in citing if c.get("status") not in CLOSED]
        # A card citing this job owns the incident; only without one does an open card about the test id.
        target = (open_citing or ([] if citing else open_for_test) or [None])[0]
        if target is not None:
            log.append(_append(ctx, {"id": target["id"], "board": None, "gateway": None}, recorded_generation))
            continue
        if citing:
            owner = citing[0]
            state["cards"][test_id] = {"card": owner["id"], "generation": recorded_generation}
            log.append(f"skipped {test_id}: {run_url} already cited by {owner['id']} ({owner.get('status')})")
            continue
        generation = recorded_generation + (1 if known else 0)
        key = f"deflake:{test_id}" + (f"#{generation}" if generation else "")
        body = _card_body(entry, run_url, branch, (known or {}).get("card"))
        log.append(_create_or_defer(ctx, f"Deflake {test_id}", body, key, generation))
    return log


class _Flake:
    """One flaky test id being filed: where its comment goes and how to tell it already landed."""

    def __init__(self, test_id: str, run_url: str, branch: str, excerpt: str, board: Board, state: dict[str, Any],
                 relay: HubRelay | None, self_gw: str | None) -> None:
        self.test_id, self.run_url, self.board, self.state, self.relay, self.self_gw = (
            test_id, run_url, board, state, relay, self_gw)
        # The test id and job URL lead the comment: a cross-gateway read-back sees only the first 500 chars.
        self.comment = f"Flaked again: `{test_id}` in {run_url} (branch `{branch}`).\n\n```\n{excerpt[-1500:]}\n```"

    def noted(self, comments: Iterable[str]) -> bool:
        return any(self.run_url in c and self.test_id in c for c in comments)

    def resolve(self, ref: dict[str, Any]) -> dict[str, Any] | None:
        """``ref`` (an overlap match or a state entry) read from its owner: this board, another board here, or
        another gateway's card in the hermes-hub replica. None when it cannot be read."""
        card_id, gw = ref.get("id") or ref.get("card"), ref.get("gateway")
        slug = ref.get("board") if ref.get("board") not in (None, "", "default") else None
        if not card_id:
            return None
        if gw in (None, "", "local", self.self_gw):
            data = self.board.card(card_id, slug) if slug else self.board.card(card_id)
            return {**data, "id": card_id, "board": slug, "gateway": None} if data else None
        data = self.relay.card(gw, slug or "default", card_id) if self.relay else None
        return {**data, "id": card_id, "board": slug or "default", "gateway": gw} if data else None

    def owns(self, card: dict[str, Any]) -> bool:
        """Open and about THIS test: its title or its body's lead paragraph names the test id and no other, or its
        key is ``deflake:<id>[#n]``. A card that only quotes the id further down (a report about this cron that
        lists several dropped tests, t_01a189b2c78e9005 itself) is not the card that fixes it."""
        return card.get("status") in OPEN and _is_about(card, self.test_id)

    def deliver(self, card: dict[str, Any]) -> tuple[bool | None, str]:
        """Comment ``card`` and read the comment back. ``(True, how)``, ``(False, why)`` or ``(None, pending)``."""
        if card.get("gateway"):
            if self.relay is None:
                return False, "no hermes-hub relay on this host"
            return self.relay.comment(card["gateway"], card["board"], card["id"], self.comment,
                                      [self.test_id, self.run_url])
        scope = (card["board"],) if card.get("board") else ()
        if self.noted(self.board.comments(card["id"], *scope)):
            return True, "already noted for this job"
        try:
            self.board.comment(card["id"], self.comment, *scope)
        except RuntimeError as exc:
            return False, str(exc)
        if self.noted(self.board.comments(card["id"], *scope)):
            return True, "comment read back"
        return False, "comment not found on read-back"

    def record(self, card: dict[str, Any], generation: int) -> None:
        entry: dict[str, Any] = {"card": card["id"], "generation": generation}
        entry.update({k: card[k] for k in ("gateway", "board") if card.get(k)})
        self.state["cards"][self.test_id] = entry

    @staticmethod
    def ref(card: dict[str, Any]) -> str:
        if card.get("gateway"):
            return f"@task:{card['board']}@{card['gateway']}/{card['id']}"
        return f"@task:{card['board']}/{card['id']}" if card.get("board") else card["id"]


def _append(ctx: _Flake, card: dict[str, Any], generation: int, why: str = "") -> str:
    """Comment an owner card; only a read-back comment logs "appended" and enters the state."""
    ok, detail = ctx.deliver(card)
    if ok:
        ctx.record(card, generation)
        return f"appended {ctx.ref(card)} {ctx.test_id}" + (f" ({why}; {detail})" if why else "")
    return f"FAILED {ctx.test_id}: comment to {ctx.ref(card)} {'not confirmed' if ok is None else 'failed'}: {detail}"


def _create_or_defer(ctx: _Flake, title: str, body: str, key: str, generation: int) -> str:
    """Create the card. On a create_overlap refusal, a match that owns this test gets the comment; matches that
    don't are passed back as ``distinct_from`` (they are other work the guard's word overlap caught)."""
    distinct: list[str] = []
    named: list[str] = []
    for _ in range(3):
        reason = (f"Flaky test {ctx.test_id} has no open card about it; the overlap matches are other work: "
                  + "; ".join(named))[:1000]
        try:
            card = ctx.board.create(title, body, key, distinct_from=distinct or None,
                                    distinct_reason=reason if distinct else None)
        except CreateRefused as refused:
            fresh = [m for m in refused.matches if m.get("id") and m["id"] not in distinct]
            if not fresh:
                return f"FAILED {ctx.test_id}: create refused with no new matches: {str(refused)[:300]}"
            for match in fresh:
                owner = ctx.resolve(match)
                if owner is not None and ctx.owns(owner):
                    return _append(ctx, owner, generation, "create refused: this open card is about the test")
            distinct += [m["id"] for m in fresh]
            named += [f"{m['id']} on {m.get('gateway') or '?'} '{(m.get('title') or '')[:80]}'" for m in fresh]
            continue
        ctx.state["cards"][ctx.test_id] = {"card": card, "generation": generation}
        return f"opened {card} {ctx.test_id}" + (f" (distinct from {', '.join(distinct)})" if distinct else "")
    return f"FAILED {ctx.test_id}: create still refused after distinct_from {', '.join(distinct)}"


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
    # Only reads are safe to replay after an ambiguous transport failure.
    attempts = 4 if req.get_method() == "GET" else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if (isinstance(exc, urllib.error.HTTPError) and exc.code < 500) or attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def _completed_runs(api: str, token: str, *, since: float, per_page: int = 100) -> list[dict[str, Any]]:
    """Every run created since ``since`` that has completed, across all result pages.

    The list is ordered by creation, but a run is only readable once it completes: a
    40-minute test run finishes after dozens of newer short runs, so a fixed first page
    (it used to be the newest 40) silently skipped exactly the long test runs that carry
    FLAKY annotations. A creation-time window, paged out, keeps them in view until they
    complete; ``state["runs"]`` dedupes across polls.
    """
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _gh(f"{api}/actions/runs?status=completed&created=%3E%3D{created}"
                    f"&per_page={per_page}&page={page}", token).get("workflow_runs", [])
        runs += batch
        if len(batch) < per_page:
            return runs
        page += 1


def poll_github(repo: str, token: str, board: Board, state: dict[str, Any], *,
                lookback_hours: float = 6.0, relay: HubRelay | None = None) -> list[str]:
    """File cards for FLAKY annotations on recently completed runs not seen before.

    ``lookback_hours`` must outlast the slowest workflow plus any cron outage, and stay
    well under ``save_state``'s 1000-run dedupe memory (the repo sees ~50 runs/hour).
    A run with a ``FAILED`` line stays unprocessed, so the next poll retries it (filed ids
    are not filed twice: a comment already on the card for this job is not repeated).
    """
    api = f"https://api.github.com/repos/{repo}"
    log: list[str] = []
    done = set(state["runs"])
    since = time.time() - lookback_hours * 3600
    for run in sorted(_completed_runs(api, token, since=since), key=lambda r: r["id"]):
        key = f"{run['id']}/{run.get('run_attempt', 1)}"
        if key in done:
            continue
        jobs = _gh(f"{api}/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100", token)
        filed: list[str] = []
        for job in jobs.get("jobs", []):
            if "Run tests" not in job.get("name", ""):
                continue
            entries = [parse_annotation(a.get("message", ""))
                       for a in _gh(f"{api}/check-runs/{job['id']}/annotations?per_page=100", token)
                       if a.get("title") == ANNOTATION_TITLE]
            if entries:
                filed += file_cards(entries, run_url=job.get("html_url") or run["html_url"],
                                    branch=run.get("head_branch") or "?", board=board, state=state, relay=relay)
        log += filed
        if not any(line.startswith("FAILED") for line in filed):
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
    ap.add_argument("--hub-cli", default="hermes-hub", help="hermes-hub CLI, for cards on other gateways")
    ap.add_argument("--hub-db", type=Path, default=None, help="hermes-hub replica (default: its state dir)")
    ap.add_argument("--hub-wait", type=float, default=90.0, help="seconds to wait for a cross-gateway comment")
    args = ap.parse_args(argv)

    if args.annotate:
        path = Path(args.annotate)
        if not path.is_file():
            print(f"flaky_kanban: no flaky report at {path} (the test step did not get that far)")
            return 0
        entries = read_report(path.read_text(encoding="utf-8-sig"))
        for line in annotations(entries):
            print(line)
        print(f"flaky_kanban: {len(entries)} flaky test id(s)")
        return 0

    board = CliBoard(args.hermes, args.assignee)
    relay = HubRelay(args.hub_db, args.hub_cli, wait=args.hub_wait)
    state = load_state(args.state)
    if args.from_github:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("flaky_kanban: GH_TOKEN/GITHUB_TOKEN not set", file=sys.stderr)
            return 2
        log = poll_github(args.repo, token, board, state, relay=relay)
    else:
        entries: list[dict[str, str]] = []
        for path in args.report:
            entries += read_report(Path(path).read_text(encoding="utf-8-sig"))
        log = file_cards(entries, run_url=args.run_url, branch=args.branch, board=board, state=state, relay=relay)
    save_state(args.state, state)
    for line in log:  # silent when nothing flaked (no-agent cron delivers stdout)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}")
    # A flake that was not filed must not look like a good run: the cron's status goes non-ok.
    return 1 if any(line.startswith("FAILED") for line in log) else 0


if __name__ == "__main__":
    sys.exit(main())
