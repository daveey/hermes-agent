#!/usr/bin/env python3
"""Journal every CI flake and batch unowned tests into one daily intake.

scripts/run_tests_parallel.py retries a failed test once in the same job. A
test that passes only on that retry keeps the job green but must never pass
silently: the runner writes it to ``--flaky-report`` JSON, the CI step
``flaky_artifact.py`` exports a metadata-only artifact (bounded test identity plus
run provenance, no failure output; scripts/ci/flaky_artifact.py) and the hub cron
downloads those artifacts into a durable metadata journal. ``--annotate`` still
prints local warning annotations (``FLAKY (passed only on retry)``) for a report.

* new unowned occurrences: one intake per repo per UTC observation day, keyed
  ``ci-flaky-intake:<repo>:<day>``, with bounded structured manifest pages
* existing exact-test/job owners: deduplicated comments, confirmed by readback;
  batching never reassigns or archives those cards
* failed/ambiguous delivery: durable retry, never advance delivery acknowledgement
  on a write receipt alone
* done intake: supported reopen; archived intake: durable carry to the next day,
  preserving original occurrence provenance rather than making a same-day replacement

See docs/ci-flaky-intake.md for backlog inspection, recovery and rollback. The
legacy file_cards helper retains per-test creation for callers without on_unowned;
production CLI admission uses the journal and daily intake instead.

Modes:

  flaky_kanban.py --annotate flaky.json               # CI step: emit annotations
  flaky_kanban.py --report flaky.json [--report ...] --run-url URL [--branch B]
  flaky_kanban.py --from-github --repo OWNER/NAME     # hub cron: poll metadata artifacts

``--from-github`` reads ``GH_TOKEN``/``GITHUB_TOKEN``, walks recently completed
workflow runs, and remembers processed run attempts in ``--state`` so each run
is filed once. Cards go through the ``hermes kanban`` CLI of the host it runs
on (the hub's board).
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import http.client
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
from typing import Any, Callable, Iterable, Protocol

try:
    from scripts.ci import flaky_artifact_poll as fap
    from scripts.ci.flaky_intake import Journal
except ImportError:  # run as a script: scripts/ci is sys.path[0]
    import flaky_artifact_poll as fap  # type: ignore[no-redef]
    from flaky_intake import Journal  # type: ignore[no-redef]

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

    def reopen(self, card_id: str) -> None:
        self._run("reopen", card_id, "--reason", "new CI flake occurrences for this daily intake")

    def comments(self, card_id: str, board: str | None = None) -> list[str]:
        data = self._show(card_id, board) or {}
        comments = data.get("comments")
        if not isinstance(comments, list) or any(
            not isinstance(c, dict) or not isinstance(c.get("body"), str) for c in comments
        ):
            raise RuntimeError("comments unavailable")
        return [c["body"] for c in comments]


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

    def require_fresh(self, gw: str, data: dict[str, Any]) -> None:
        from hermes_cli.kanban_scope_heartbeat import fresh_scopes, scope_heartbeats

        parts = str(data.get("_route") or "").split("/")
        rows = self._query("SELECT gw,data FROM replica WHERE coll='gateway' AND key='info'")
        scopes = fresh_scopes(scope_heartbeats(rows), time.time())
        if len(parts) != 3 or (gw, parts[0], parts[1]) not in scopes:
            raise RuntimeError("owner scope freshness unavailable")

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
        "Usual cause on the CI runners: a fixed wall-clock deadline (sleep, short wait/join timeout, short",
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
    if excerpt:
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
    def exact_url(value: str) -> bool:
        return re.search(r"(?<![\w/])" + re.escape(value) + r"(?![\w/#?%=&+\-])", text) is not None

    if exact_url(url):
        return True
    m = _GH_JOB_URL.fullmatch(url)
    return bool(m) and exact_url(m.group(1)) and re.search(rf"\bjob\s+`?{m.group(2)}(?!\w)", text) is not None


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
               state: dict[str, Any], relay: HubRelay | None = None,
               on_unowned: Callable[[], str] | None = None,
               owner_notes: dict[str, list[dict[str, str]]] | None = None) -> list[str]:
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
        ctx = _Flake(test_id, run_url, branch, entry.get("first_attempt_excerpt") or "", board, state, relay, self_gw,
                     require_fresh=on_unowned is not None)
        known = state["cards"].get(test_id)
        recorded_generation = (known or {}).get("generation", 0)
        owner = ctx.resolve(known) if known else None
        if on_unowned is not None and known and (owner is None or owner.get("status") is None):
            if not ctx.cached_owner_absent(known):
                log.append(f"FAILED {test_id}: cached owner unreadable; durable retry pending")
                continue
            del state["cards"][test_id]
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
        if on_unowned is not None:
            foreign = _foreign_owner(ctx)
            if foreign is not None and foreign.get("status") in CLOSED:
                ctx.record(foreign, recorded_generation)
                log.append(f"skipped {test_id}: {run_url} already cited by {ctx.ref(foreign)} "
                           f"({foreign['status']})")
            elif foreign is not None:
                log.append(_append(ctx, foreign, recorded_generation, "existing exact owner"))
            else:
                if owner_notes is not None:
                    owner_notes[test_id] = list(ctx.owner_notes)
                log.append(on_unowned())
            continue
        generation = recorded_generation + (1 if known else 0)
        key = f"deflake:{test_id}" + (f"#{generation}" if generation else "")
        body = _card_body(entry, run_url, branch, (known or {}).get("card"))
        log.append(_create_or_defer(ctx, f"Deflake {test_id}", body, key, generation))
    return log


class _Flake:
    """One flaky test id being filed: where its comment goes and how to tell it already landed."""

    def __init__(self, test_id: str, run_url: str, branch: str, excerpt: str, board: Board, state: dict[str, Any],
                 relay: HubRelay | None, self_gw: str | None, *, require_fresh: bool = False) -> None:
        self.test_id, self.run_url, self.board, self.state, self.relay, self.self_gw = (
            test_id, run_url, board, state, relay, self_gw)
        self.require_fresh = require_fresh
        # Unverifiable prior-owner candidates, named in the intake manifest for triage.
        self.owner_notes: list[dict[str, str]] = []
        # The test id and job URL lead the comment: a cross-gateway read-back sees only the first 500 chars.
        self.comment = f"Flaked again: `{test_id}` in {run_url} (branch `{branch}`)." + (
            f"\n\n```\n{excerpt[-1500:]}\n```" if excerpt else "")

    def cached_owner_absent(self, ref: dict[str, Any]) -> bool:
        """Only a complete local inventory proves a cached owner is gone.

        Replica absence or repeated read failures are not deletion receipts.
        Other boards require operator reconciliation on their owning scope.
        """
        if ref.get("gateway") not in (None, "", "local", self.self_gw):
            return False
        if ref.get("board") not in (None, "", "default"):
            return False
        return not any(c.get("id") == ref.get("card") for c in self.board.list_cards())

    def noted(self, comments: Iterable[str]) -> bool:
        return any(_cites_job(c, self.run_url) and _mentions(c, self.test_id) for c in comments)

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
        if data and self.require_fresh:
            self.relay.require_fresh(gw, data)
        return {**data, "id": card_id, "board": slug or "default", "gateway": gw} if data else None

    def owns(self, card: dict[str, Any]) -> bool:
        """Open and about THIS test: its title or its body's lead paragraph names the test id and no other, or its
        key is ``deflake:<id>[#n]``. A card that only quotes the id further down (a report about this cron that
        lists several dropped tests, t_01a189b2c78e9005 itself) is not the card that fixes it."""
        return card.get("status") in OPEN and _is_about(card, self.test_id)

    def deliver(self, card: dict[str, Any]) -> tuple[bool | None, str]:
        """Comment ``card`` and read the comment back. ``(True, how)``, ``(False, why)`` or ``(None, pending)``."""
        # Legacy owners put the initial job receipt in the body, not a comment.
        current = self.resolve(card) if self.require_fresh else None
        if current is not None and self.noted([str(current.get("body") or "")]):
            return True, "already noted in owner body for this job"
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


def _foreign_owner(ctx: _Flake) -> dict[str, Any] | None:
    """Enumerate replica owners before batching; retain the exact ownership predicate."""
    if ctx.relay is None:
        return None
    if not ctx.relay.db.is_file():
        raise RuntimeError("owner inventory unavailable")
    conn = sqlite3.connect(f"file:{ctx.relay.db}?mode=ro", uri=True, timeout=5)
    try:
        refs = conn.execute("SELECT gw,key,data FROM replica WHERE coll='tasks'").fetchall()
    finally:
        conn.close()
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    uncertain: list[dict[str, str]] = []
    unavailable: RuntimeError | None = None
    for gw, key, raw in refs:
        data = json.loads(raw)
        if not isinstance(data, dict) or not _is_about(data, ctx.test_id):
            continue
        parts = key.split("/")
        if len(parts) not in (2, 3):
            continue
        try:
            owner = ctx.resolve({"id": parts[-1], "board": parts[-2], "gateway": gw})
        except RuntimeError as exc:
            unavailable = exc
            continue
        if owner is None or owner.get("status") not in OPEN | CLOSED:
            unavailable = RuntimeError("owner readback unavailable")
            continue
        text = f"{owner.get('title') or ''}\n{owner.get('body') or ''}\n" + "\n".join(
            str(c.get("body") or "") for c in owner.get("last_comments") or [])
        cites = _cites_job(text, ctx.run_url)
        if ctx.owns(owner) or (owner.get("status") in CLOSED and cites):
            rank = (0 if owner.get("status") in OPEN else 1) if cites else 2
            candidates.append((rank, ctx.ref(owner), owner))
        elif owner.get("status") in CLOSED and not _complete_foreign_history(owner):
            # Clipped history cannot prove this closed card never cited the job.
            # Holding the row would strand every recurrence of a test whose old
            # card is closed with >3 comments; the intake names it for triage.
            uncertain.append({"candidate": ctx.ref(owner), "status": str(owner["status"]),
                              "reason": "closed_history_incomplete"})
    # Match local precedence, independent of replica row order. Unknown history
    # cannot erase a known positive, but it must never become absence evidence.
    if candidates:
        return min(candidates, key=lambda candidate: candidate[:2])[2]
    if unavailable is not None:
        raise unavailable
    ctx.owner_notes = sorted(uncertain, key=lambda note: note["candidate"])
    return None


def _complete_foreign_history(owner: dict[str, Any]) -> bool:
    """Only an untruncated replica can establish absence of a closed-job receipt.

    The producer publishes a total count and last three comments, clips comments
    at 500 characters, body at 4000 and title at 300. Boundary lengths are
    deliberately conservative: they cannot prove an untruncated source.
    """
    comments, total = owner.get("last_comments"), owner.get("comments")
    return (type(total) is int and isinstance(comments, list) and total == len(comments)
            and all(isinstance(c, dict) and isinstance(c.get("body"), str)
                    and len(c["body"]) < 500 for c in comments)
            and all(isinstance(owner.get(k), str) and len(owner[k]) < limit
                    for k, limit in (("body", 4000), ("title", 300))))


def drain_intake(journal: Journal, repo: str, board: Board, state: dict, relay: HubRelay | None,
                 *, deadline: float | None = None) -> list[str]:
    log = []
    # Retain retry batch boundaries when rechecking never-attempted pages. A slow
    # unrelated owner must not repeatedly consume the budget before this prefix
    # can be sent. Ticket ordering remains unchanged.
    retry_groups = dict(journal.db.execute(
        "SELECT o.id,o.page FROM occurrences o JOIN pages p ON p.id=o.page "
        "WHERE o.repo=? AND p.attempted=0 AND p.delivered=0", (repo,)))
    journal.recheck_unattempted(repo)
    # Reserve half the drain for retry pages. Bound admission batches independently
    # of the backlog; every flush commits its pages before attempting delivery.
    now = time.monotonic()
    resolve_deadline = None if deadline is None else now + max(0, deadline - now) / 2
    old_pages = {r[0] for r in journal.db.execute(
        "SELECT id FROM pages WHERE repo=? AND delivered=0", (repo,))}
    def create(title, body, key, page):
        return _create_intake(journal, repo, board, state, relay, title, body, key, page, deadline)
    batch, owner_checks = [], {}
    def flush():
        try:
            journal.plan_pages(repo, batch, owner_check=owner_checks)
        except ValueError:
            # An oversized owner inventory must not poison the valid prefix.
            for row in batch:
                try:
                    journal.plan_pages(repo, [row], owner_check=owner_checks)
                except ValueError:
                    log.append(f"FAILED intake_page_oversize occurrence={row['id']}; durable retry pending")
        page_ids = {journal.db.execute("SELECT page FROM occurrences WHERE id=?", (row["id"],)).fetchone()[0]
                    for row in batch}
        batch.clear()
        owner_checks.clear()
        return journal.deliver(repo, board, deadline=deadline, create=create, page_ids=page_ids)
    previous_group = None
    for row in journal.pending(repo):
        group = retry_groups.get(row["id"])
        if group != previous_group:
            log += flush()
        previous_group = group
        if resolve_deadline is not None and time.monotonic() >= resolve_deadline:
            log.append("FAILED intake_budget; durable retry pending")
            break
        journal.rotate(repo, "occurrence", str(row["id"]))
        meta = json.loads(row["metadata"])
        url = f"https://github.com/{repo}/actions/runs/{meta['run_id']}/job/{meta['job_id']}"
        notes: dict[str, list[dict[str, str]]] = {}
        try:
            lines = file_cards([meta], run_url=url, branch=meta["head_sha"], board=board, state=state,
                               relay=relay, on_unowned=lambda: "queued", owner_notes=notes)
        except (RuntimeError, subprocess.TimeoutExpired):
            log += flush()
            log.append(f"FAILED {meta['test_id']}: owner delivery unavailable; durable retry pending")
            continue
        if lines == ["queued"]:
            batch.append(row)
            owner_checks[row["id"]] = notes.get(meta["test_id"], [])
            if len(batch) >= 16:
                log += flush()
        else:
            log += flush()
            if lines and not any(line.startswith("FAILED") for line in lines):
                journal.acknowledge(row["id"], json.dumps(state["cards"][meta["test_id"]], sort_keys=True))
        log += lines
    log += flush()
    return log + journal.deliver(repo, board, deadline=deadline, create=create, page_ids=old_pages)


def _create_intake(journal, repo, board, state, relay, title, body, key, page, deadline):
    """Reconcile definitive overlap refusals without dissolving ambiguous pages."""
    distinct = []
    rows = journal.db.execute("SELECT metadata FROM occurrences WHERE page=?", (page["id"],)).fetchall()
    contexts = []
    for row in rows:
        meta = json.loads(row[0])
        url = f"https://github.com/{repo}/actions/runs/{meta['run_id']}/job/{meta['job_id']}"
        contexts.append(_Flake(meta["test_id"], url, meta["head_sha"], "", board, state,
                               relay, relay.self_gateway() if relay else None, require_fresh=True))
    for _ in range(3):
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError("intake_budget")
        try:
            with journal.db:
                journal.db.execute("UPDATE pages SET attempted=1 WHERE id=?", (page["id"],))
            if not distinct:
                return board.create(title, body, key)
            return board.create(title, body, key, distinct_from=distinct,
                                distinct_reason="Daily intake only admits unowned occurrences; "
                                "read-back overlap matches do not own any test in this manifest.")
        except CreateRefused as refused:
            # deliver() proved the key absent before calling us. Keep the old
            # ambiguity fence until every overlap match has also been read back.
            if not contexts:
                raise
            fresh = [m for m in refused.matches if m.get("id") and m["id"] not in distinct]
            if not fresh:
                raise
            owners = []
            for match in fresh:
                if deadline is not None and time.monotonic() >= deadline:
                    raise RuntimeError("intake_budget")
                owner = contexts[0].resolve(match)
                if owner is None or owner.get("status") not in OPEN | CLOSED:
                    # Preserve earlier ambiguity when match readback is unavailable.
                    if not page["attempted"]:
                        journal.unplan_page(page["id"])
                    raise RuntimeError("intake overlap owner unreadable") from None
                owners.append(owner)
            exact = [(ctx, owner) for ctx in contexts for owner in owners if _overlap_owns(ctx, owner)]
            if exact:
                for ctx, owner in exact:
                    if ctx.owns(owner):
                        generation = state["cards"].get(ctx.test_id, {}).get("generation", 0)
                        ctx.record(owner, generation)
                journal.unplan_page(page["id"])
                return None
            distinct.extend(m["id"] for m in fresh)
    raise RuntimeError("intake overlap still refused")


def _overlap_owns(ctx: _Flake, owner: dict[str, Any]) -> bool:
    if not _is_about(owner, ctx.test_id):
        return False
    if ctx.owns(owner):
        return True
    text = f"{owner.get('title') or ''}\n{owner.get('body') or ''}"
    if _cites_job(text, ctx.run_url):
        return True
    if owner.get("gateway"):
        history = "\n".join(str(c.get("body") or "") for c in owner.get("last_comments") or [])
        if _cites_job(history, ctx.run_url):
            return True
        if not _complete_foreign_history(owner):
            raise RuntimeError("closed foreign owner history unavailable")
        return False
    scope = (owner["board"],) if owner.get("board") else ()
    return _cites_job("\n".join(ctx.board.comments(owner["id"], *scope)), ctx.run_url)


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
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("invalid flaky report")
    entries = data.get("flaky", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("invalid flaky report")
    return entries


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


# --- Hub side: metadata artifacts -> cards (cron) ---------------------------------------


def _gh(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-ci-flaky-cards"})
    # Only reads are safe to replay after an ambiguous transport failure.
    attempts = 4 if req.get_method() == "GET" else 1
    for attempt in range(attempts):
        try:
            _, _, body = fap.fa.transport.read(req, 16 * 1024 * 1024, fap.fa._NoRedirect())
            if len(body) > 16 * 1024 * 1024:
                raise ValueError("GitHub response exceeds size limit")
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if (isinstance(exc, urllib.error.HTTPError) and exc.code < 500) or attempt == attempts - 1:
                raise
            time.sleep(fap.fa.transport.remaining(2 ** attempt))


def _completed_runs(api: str, token: str, *, since: float, per_page: int = 100) -> list[dict[str, Any]]:
    """Every run created since ``since`` that has completed, across all result pages.

    The list is ordered by creation, but a run is only readable once it completes: a
    40-minute test run finishes after dozens of newer short runs, so a fixed first page
    (it used to be the newest 40) silently skipped exactly the long test runs that carry
    FLAKY annotations. A creation-time window, paged out, keeps them in view until they
    complete; ``state["runs"]`` dedupes across polls.

    Bounded and validated: at most ``fap.MAX_PAGES`` pages (a listing that is still full after
    that is rejected, never followed forever), each page a list of at most ``per_page`` run
    objects with in-range integer ``id``/``run_attempt``. Any other shape raises ``ValueError``,
    which ``poll_github`` reports as the fixed ``runs_unavailable`` receipt.
    """
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    runs: list[dict[str, Any]] = []
    for page in range(1, fap.MAX_PAGES + 1):
        body = _gh(f"{api}/actions/runs?status=completed&created=%3E%3D{created}"
                   f"&per_page={per_page}&page={page}", token)
        batch = body.get("workflow_runs") if isinstance(body, dict) else None
        if not isinstance(batch, list) or len(batch) > per_page:
            raise ValueError("runs_unavailable")
        for run in batch:
            if not (isinstance(run, dict) and fap.fa._int_in(run.get("id"), 1, fap.fa.MAX_INT)
                    and fap.fa._int_in(run.get("run_attempt", 1), 1, fap.fa.MAX_INT)):
                raise ValueError("runs_unavailable")
        runs += batch
        if len(batch) < per_page:
            return runs
    raise ValueError("runs_unavailable")


def _filing(result: Any, run: dict[str, Any], repo: str, board: Board, state: dict[str, Any],
            relay: HubRelay | None) -> list[str]:
    """Card lines for every slice of a fully validated run. Job URLs come from validated numeric ids only."""
    lines: list[str] = []
    for job_id, entries in result.jobs:
        if entries:
            url = f"https://github.com/{repo}/actions/runs/{run['id']}/job/{job_id}"
            lines += file_cards(entries, run_url=url, branch=run["head_sha"], board=board, state=state, relay=relay)
    return lines


def _poll_run(repo: str, token: str, run: dict[str, Any], board: Board, state: dict[str, Any],
              relay: HubRelay | None, journal: Journal | None = None) -> tuple[str, list[str], str | None]:
    """``(outcome, log lines, unavailable code)``; outcome is complete|empty|not_applicable|unavailable
    (unavailable = retry later). The code is set when the run's data could not be read (``fap.Unavailable``);
    ``poll_github`` turns it into a RETRY/WAITING/FAILED line by how long it has persisted."""
    key = f"{run['id']}/{run.get('run_attempt', 1)}"
    try:
        result = fap.assess_run(repo, run, lambda url: _gh(url, token), token)
    except fap.Unavailable as exc:
        return "unavailable", [], exc.code
    if result.kind == "not_applicable":
        state["runs"].append(key)
        return "not_applicable", [], None
    if journal is not None:
        journal.record(repo, result.jobs, run)
        lines = []
    else:
        lines = _filing(result, run, repo, board, state, relay)
    if any(line.startswith("FAILED") for line in lines):
        return "unavailable", lines, None
    state["runs"].append(key)
    return ("complete" if any(entries for _, entries in result.jobs) else "empty"), lines, None


GRACE_SECONDS = 15 * 60     # artifacts are listed before their job even completes; 15 min is ample margin
MAX_TRACKED = 1000


def _age(run: dict[str, Any], now: float) -> float | None:
    """Seconds since the run (attempt) completed, from its ``updated_at``; None when unreadable."""
    stamp = run.get("updated_at")
    if not (isinstance(stamp, str) and fap._STAMP.fullmatch(stamp)):
        return None
    try:
        return now - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _unavailable_line(key: str, code: str, run: dict[str, Any], tracked: dict[str, Any], seq: int,
                      now: float, grace: float) -> str:
    """Fresh ``missing`` (completed under ``grace`` ago) is ``RETRY``; an attempt unavailable in this and the
    previous poll and completed at least ``grace`` ago is ``FAILED`` (the cron goes red); else ``WAITING``."""
    prev = tracked.get(key)
    before = prev.get("polls") if isinstance(prev, dict) and prev.get("seq") == seq - 1 else 0
    polls = (before if isinstance(before, int) and not isinstance(before, bool) and before > 0 else 0) + 1
    tracked[key] = {"seq": seq, "polls": polls}
    age = _age(run, now)
    if code == "missing" and age is not None and age < grace:
        return f"RETRY run={key} code={code} age={int(age)}s"
    if polls >= 2 and not (age is not None and age < grace):
        return f"FAILED run={key} code={code} polls={polls}"
    return f"WAITING run={key} code={code} polls={polls}"


@fap.fa.transport.scoped_poll
def poll_github(repo: str, token: str, board: Board, state: dict[str, Any], *,
                lookback_hours: float = 6.0, relay: HubRelay | None = None,
                deadline: float | None = None, checkpoint: Callable[[], None] | None = None,
                grace: float = GRACE_SECONDS, journal: Journal | None = None) -> list[str]:
    """File cards from the metadata artifacts of recently completed runs not seen before.

    ``lookback_hours`` must outlast the slowest workflow plus any cron outage, and stay
    well under ``save_state``'s 1000-run dedupe memory (the repo sees ~50 runs/hour).
    A run whose artifacts are unavailable, or whose card filing failed, stays unprocessed
    so the next poll retries it; filed ids are not filed twice. A card filing failure is a
    ``FAILED`` line at once. Unreadable data (``fap.Unavailable``) is logged by persistence
    (``state["unavailable"]``: attempt -> poll sequence and consecutive unavailable polls,
    pruned to attempts still listed and unprocessed, so it stays bounded):

    * ``RETRY run=<k> code=missing age=<s>s``: completed under ``grace`` ago; the artifact may
      not be listed yet.
    * ``WAITING run=<k> code=<c> polls=<n>``: unavailable for the first poll in a row, or still
      inside ``grace``.
    * ``FAILED run=<k> code=<c> polls=<n>``: unavailable in this and the previous poll and
      completed at least ``grace`` ago. Only this one turns the cron red.
    * ``FAILED run=<k> code=<c> polls=<n> retired=unlisted`` (journal only): the same, for a stored
      run that a successful listing no longer includes. The run is marked complete, so this
      terminal line appears once; a failed listing never retires anything.

    Cancelled/timed-out jobs and incomplete reports remain unavailable and unprocessed, except a
    cancelled run superseded by a newer run of its workflow and branch (``not_applicable``). Every
    expected slice must supply a valid artifact before any card is touched. When a run was newly
    observed the log ends with ``POLL complete=<n> empty=<n> unavailable=<n> not_applicable=<n>``
    (``complete`` counts every run with readable data, ``empty`` the subset with no flakes).

    The cron wrapper kills a poll that overruns its timeout and drops all of that poll's results, so
    while GitHub is slow every poll would redo and lose the same work. ``checkpoint`` runs after each
    run (``main`` saves the state there), and no run starts once ``time.monotonic()`` reaches
    ``deadline``: the rest stay unprocessed behind a ``FAILED poll_budget deferred=<n>`` line.
    """
    if not isinstance(repo, str) or fap.fa._REPO.fullmatch(repo) is None:
        return ["FAILED runs_unavailable"]
    api = f"https://api.github.com/repos/{repo}"
    done = set(state["runs"])
    since = time.time() - lookback_hours * 3600
    log: list[str] = []
    listed: set[str] | None = None      # keys GitHub listed this poll; None when the listing failed
    try:
        runs = _completed_runs(api, token, since=since)
        listed = {f"{r['id']}/{r.get('run_attempt', 1)}" for r in runs}
    except (OSError, ValueError, http.client.HTTPException):
        if journal is None:
            return ["FAILED runs_unavailable"]
        runs = []
        log.append("FAILED runs_unavailable")
    if journal is not None:
        runs = journal.remember_runs(repo, runs)
        # The durable journal is authoritative. A crash between the JSON checkpoint
        # and journal completion must not strand an unfinished stored descriptor.
        done = set()
    counts = {"complete": 0, "empty": 0, "unavailable": 0, "not_applicable": 0}
    now = time.time()
    prior_seq = state.get("poll_seq")
    seq = state["poll_seq"] = (prior_seq if isinstance(prior_seq, int) else 0) + 1
    prior = state.get("unavailable")
    tracked: dict[str, Any] = prior if isinstance(prior, dict) else {}
    keys = [f"{r['id']}/{r.get('run_attempt', 1)}" for r in runs]
    ordered = runs if journal is not None else sorted(runs, key=lambda r: r["id"])
    fresh = [r for r in ordered if f"{r['id']}/{r.get('run_attempt', 1)}" not in done]
    # Bounded: only attempts still listed and unprocessed are remembered.
    state["unavailable"] = tracked = {k: tracked[k] for k in keys if k in tracked and k not in done}
    for index, run in enumerate(fresh):
        if deadline is not None and time.monotonic() >= deadline:
            log.append(f"FAILED poll_budget deferred={len(fresh) - index}")
            break
        key = f"{run['id']}/{run.get('run_attempt', 1)}"
        if journal is not None:
            journal.rotate(repo, "run", key)
        outcome, lines, code = _poll_run(repo, token, run, board, state, relay, journal)
        if journal is not None and outcome != "unavailable":
            journal.finish_run(repo, key)
        if code is not None:
            if journal is not None:
                tracked[key] = {"seq": seq - 1, "polls": journal.note_unavailable(repo, key) - 1}
            line = _unavailable_line(key, code, run, tracked, seq, now, grace)
            if journal is not None and listed is not None and key not in listed and line.startswith("FAILED"):
                # A stored run that a successful listing no longer includes, still unavailable past grace,
                # is retired with one terminal line, as the non-journal path drops it from the listing.
                line += " retired=unlisted"
                journal.finish_run(repo, key)
                tracked.pop(key, None)
            lines = [line]
        else:
            tracked.pop(key, None)
        log += lines
        counts[outcome] += 1
        counts["complete"] += outcome == "empty"    # an empty result is a complete one
        if checkpoint is not None:
            checkpoint()
    if len(tracked) > MAX_TRACKED:
        newest = sorted(tracked, key=lambda k: int(k.split("/")[0]))[-MAX_TRACKED:]
        state["unavailable"] = {k: tracked[k] for k in newest}
    if fresh:
        log.append("POLL " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--annotate", metavar="REPORT", help="CI: print one FLAKY annotation per report entry")
    ap.add_argument("--report", action="append", default=[], help="flaky report JSON (repeatable)")
    ap.add_argument("--run-url", default="(local run)")
    ap.add_argument("--branch", default="?")
    ap.add_argument("--from-github", action="store_true")
    ap.add_argument("--repo", default="softmax-universe/daveey-hermes")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--initialize-intake", action="store_true", help="one-time journal bootstrap; never polls")
    ap.add_argument("--intake-identity", type=Path, help="independently retained journal identity anchor")
    ap.add_argument("--hermes", default=os.environ.get("HERMES_BIN", "hermes"))
    ap.add_argument("--assignee", default="default")
    ap.add_argument("--hub-cli", default="hermes-hub", help="hermes-hub CLI, for cards on other gateways")
    ap.add_argument("--hub-db", type=Path, default=None, help="hermes-hub replica (default: its state dir)")
    ap.add_argument("--hub-wait", type=float, default=90.0, help="seconds to wait for a cross-gateway comment")
    ap.add_argument("--budget", type=float, default=420.0,
                    help="--from-github: seconds after which no further run is started (the cron kills it at 600)")
    ap.add_argument("--grace-minutes", type=float, default=GRACE_SECONDS / 60,
                    help="--from-github: a run completed this recently may still be listing its artifacts")
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
    journal = None
    try:
        queue = args.state.with_suffix(".intake.sqlite3").resolve()
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        identity = args.intake_identity or (home / "intake-identities" /
                    (hashlib.sha256(str(queue).encode("utf-8")).hexdigest() + ".uuid"))
        journal = Journal(queue, identity_path=identity, initialize=args.initialize_intake)
        if args.initialize_intake:
            print("Intake initialized; preserve journal and independent identity anchor")
            return 0
        return _run_intake(args, board, relay, journal)
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
        print("FAILED intake unavailable; durable retry pending", file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()


def _run_intake(args: argparse.Namespace, board: Board, relay: HubRelay, journal: Journal) -> int:
    state = load_state(args.state)
    deadline = time.monotonic() + args.budget
    if args.from_github:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("flaky_kanban: GH_TOKEN/GITHUB_TOKEN not set", file=sys.stderr)
            return 2
        # Persisted delivery gets a bounded first turn even when discovery is slow.
        # The remaining budget still belongs to CI assessment; network failures
        # remain red and newly discovered rows can drain on the next invocation.
        log = drain_intake(journal, args.repo, board, state, relay,
                           deadline=time.monotonic() + max(0, args.budget) / 2)
        save_state(args.state, state)
        log += poll_github(args.repo, token, board, state, relay=relay,
                          deadline=deadline, grace=args.grace_minutes * 60,
                          checkpoint=lambda: save_state(args.state, state), journal=journal)
    else:
        entries: list[dict[str, str]] = []
        for path in args.report:
            entries += read_report(Path(path).read_text(encoding="utf-8-sig"))
        match = _GH_JOB_URL.fullmatch(args.run_url)
        if match is None or not args.run_url.startswith(f"https://github.com/{args.repo}/"):
            raise ValueError("intake requires a repository job URL")
        if not fap.fa._SHA.fullmatch(args.branch) or any(
            fap.fa.canonical_test_id(e.get("test_id")) != e.get("test_id") for e in entries
        ):
            raise ValueError("invalid intake metadata")
        run_id = int(match.group(1).rsplit("/", 1)[1])
        journal.record(args.repo, [(int(match.group(2)), entries)], {"id": run_id, "head_sha": args.branch})
        log = []
    log += drain_intake(journal, args.repo, board, state, relay, deadline=deadline)
    journal.prune(args.repo)
    save_state(args.state, state)
    for line in log:  # silent when nothing flaked (no-agent cron delivers stdout)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}")
    # A flake that was not filed, or data that stayed unreadable across polls, must not look like a good run:
    # the cron's status goes non-ok. RETRY/WAITING lines (not yet persistent) do not.
    return 1 if any(line.startswith("FAILED") for line in log) else 0


if __name__ == "__main__":
    sys.exit(main())
