"""Immediate review of an ordinary spend stop, so a stopped card never waits for an idle chat. [daveey]

Before this, a spend stop (``kanban_spend_budget.hold``) asked the creator chat to decide, or opened a
decision card when no chat was live. Over 2026-11-17..24 the hub saw 488 stops: chats took a median 3.8 min
but a 90th percentile of 4.3 h, decision cards (queued behind every other card) 12 min / 7.5 h, and 33 cards
sat for up to 40 h. The reviewer decides at once, outside the worker queue:

* **Queue.** When the home board routes a new ordinary stop (``kanban_spend_decision.ensure_producer``), an
  eligible card gets ``spend_review_queued`` instead of a chat notice or decision card. A stop already routed
  to a chat or decision card that has not been decided for ``BACKLOG_AFTER`` seconds is queued too. Each stop
  (episode) gets at most one review.
* **Run.** Every dispatcher tick (``start_due``, from the maintenance sweep, before the worker cap) starts due
  reviews in daemon threads, at most ``MAX_CONCURRENT`` per process, with a ``LEASE_SECONDS`` lease
  (``spend_review_started``); the judge call is bounded by ``TIMEOUT`` end to end, an expired lease is retried,
  and after ``MAX_ATTEMPTS`` the stop is escalated. A one-shot ``hermes kanban dispatch`` waits for the reviews
  it started (``wait_for_reviews``). Turning the reviewer off releases queued and running stops to the usual
  routing.
* **Judge.** One subscription-model call reads the card, its stop, the runs and comments of the stopped
  window, and answers ``grant`` (N runs) or ``escalate``.
* **Queue only** an eligible stop: the reviewer is on, the stop is ordinary (not scope or safety), the card is a
  home card and not itself a decision card, no closed decision card already disposed of the stop, and the card
  has had fewer than ``kanban.spend_review_max_grants`` (default 2) reviewer grants since the last decision
  someone else made. One review per stop, so these hold for the grant too.
* **Grant** only when code also checks: N is at most the allowance that just ran out (and
  ``kanban_spend_promote.MAX_RUNS``); the judge quoted at least ``QUOTE_MIN`` characters verbatim from that
  window's run summaries or non-system comments as progress; and, again under the write lock, the stop is still
  the card's current, ordinary, pending, undisposed stop, still in triage, with no human comment since and no
  open parent. The grant is the decision card's promotion (``kanban_spend_promote.apply``):
  status ready, a ``spend_budget_set`` of exactly N runs, a ``promoted_manual`` that opens a fresh window,
  and a comment. An open decision card for the same stop is archived as decided.
* **Escalate** otherwise (``spend_review_escalated`` plus a comment with the reason): the stop is routed the
  usual way, so the creator chat is woken or a decision card is opened.

``kanban.spend_review: false`` turns the reviewer off.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

QUEUED = "spend_review_queued"
STARTED = "spend_review_started"
GRANTED = "spend_review_granted"
ESCALATED = "spend_review_escalated"
KINDS = (QUEUED, STARTED, GRANTED, ESCALATED)
AUTHOR = "kanban-spend-reviewer"
PROVIDER = "anthropic"
MODEL = "claude-sonnet-5-5"
TIMEOUT = 90
LEASE_SECONDS = 300
MAX_ATTEMPTS = 2
MAX_CONCURRENT = 3
BACKLOG_AFTER = 1800
DEFAULT_MAX_GRANTS = 2
QUOTE_MIN = 30
_TEXT_MAX = 1500

_INFLIGHT: set[tuple[str, str]] = set()
_LOCK = threading.Lock()
_THREADS: list = []


def settings(cfg: Optional[dict] = None) -> tuple[bool, int]:
    """``kanban.spend_review`` (default on) and ``kanban.spend_review_max_grants`` (default 2)."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:  # health: allow BLE001 -- an unreadable config keeps the reviewer's defaults
            logger.warning("kanban: spend reviewer could not read config; using defaults", exc_info=True)
            cfg = {}
    section = (cfg or {}).get("kanban") or {}
    enabled = section.get("spend_review", True) is not False
    raw = section.get("spend_review_max_grants", DEFAULT_MAX_GRANTS)
    grants = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else DEFAULT_MAX_GRANTS
    return enabled, grants


def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _review_events(conn: sqlite3.Connection, task_id: str, episode: str) -> list[dict]:
    rows = conn.execute(f"SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? AND kind IN "
                        f"({','.join('?' * len(KINDS))}) ORDER BY id", (task_id, *KINDS)).fetchall()
    return [{"id": r[0], "kind": r[1], "payload": _payload(r[2]), "created_at": r[3]} for r in rows
            if _payload(r[2]).get("episode") == episode]


def state(conn: sqlite3.Connection, task_id: str, episode: str) -> Optional[str]:
    """The newest review event kind for this stop, or None when it was never queued."""
    events = _review_events(conn, task_id, episode)
    return events[-1]["kind"] if events else None


def holds(conn: sqlite3.Connection, task_id: str, episode: str) -> bool:
    """While a review is queued or running the stop needs no other decision route. With the reviewer off, a
    queued or running review holds nothing: the stop routes as usual."""
    return state(conn, task_id, episode) in (QUEUED, STARTED) and settings()[0]


def episode_of(stop: Optional[dict]) -> Optional[str]:
    return (stop.get("uid") or str(stop["id"])) if stop else None


def _by_reviewer(kind: str, payload: dict) -> bool:
    who = payload.get("by") if kind == "spend_budget_set" else payload.get("actor")
    return who == AUTHOR


def consecutive_grants(conn: sqlite3.Connection, task_id: str) -> int:
    """Reviewer grants since the last budget decision anyone else recorded."""
    count = 0
    for kind, raw in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN (?, 'spend_budget_set', "
            "'promoted_manual') ORDER BY id DESC", (task_id, GRANTED)).fetchall():
        payload = _payload(raw)
        if kind == GRANTED:
            count += 1
        elif not _by_reviewer(kind, payload):
            break
    return count


def eligible(conn: sqlite3.Connection, task_id: str, episode: str, cfg: Optional[tuple[bool, int]] = None) -> bool:
    """An ordinary, undisposed stop on a home card that has not been reviewed and is under the grant cap."""
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_refusal_receipt import restricted
    from hermes_cli.kanban_wake_check import foreign_copy
    enabled, max_grants = cfg or settings()
    if not enabled or state(conn, task_id, episode) is not None or foreign_copy(conn, task_id):
        return False
    if decision.execution_restricted(conn, task_id) or restricted(conn, task_id) or _disposed(conn, task_id, episode):
        return False
    stop = decision.pending_episode(conn, task_id)
    if stop is not None and _payload(stop.get("payload")).get("cause"):
        return False  # e.g. capability_unavailable: more runs meet the same wall, so there is no budget to judge
    row = conn.execute("SELECT created_by FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row[0] == "kanban-spend-budget" or conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'administrative_decision'", (task_id,)).fetchone():
        return False  # a decision card that ran out decides nothing about itself
    return consecutive_grants(conn, task_id) < max_grants


def _disposed(conn: sqlite3.Connection, task_id: str, episode: str) -> bool:
    """A closed decision card for this stop is a disposition (``episode_route`` kind ``dismissed``), not release."""
    from hermes_cli.kanban_spend_decision import episode_route
    return (episode_route(conn, task_id, episode) or {}).get("kind") == "dismissed"


def queue(conn: sqlite3.Connection, task_id: str, episode: str, *, backlog: bool = False,
          cfg: Optional[tuple[bool, int]] = None) -> bool:
    """Inside the caller's write transaction: queue one review of ``episode`` when eligible."""
    from hermes_cli import kanban_db as kb
    if not eligible(conn, task_id, episode, cfg):
        return False
    kb._append_event(conn, task_id, QUEUED, {"episode": episode, "backlog": backlog})
    return True


# --- dispatcher side ---------------------------------------------------------------------------------------------

def _db_path(conn: sqlite3.Connection) -> str:
    return next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")


def _current_episode(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    from hermes_cli import kanban_spend_decision as decision
    return episode_of(decision.pending_episode(conn, task_id))


def _notice_at(conn: sqlite3.Connection, task_id: str, episode: str) -> Optional[int]:
    from hermes_cli.kanban_spend_decision import NOTICE
    row = conn.execute("SELECT MIN(created_at) FROM task_events WHERE task_id = ? AND kind = ? AND json_valid(payload) "
                       "AND json_extract(payload, '$.episode') = ?", (task_id, NOTICE, episode)).fetchone()
    return row[0] if row else None


def queue_backlog(conn: sqlite3.Connection, *, now: int, cfg: tuple[bool, int],
                  skip: frozenset | set = frozenset()) -> list[str]:
    """Stops routed to a chat or decision card that nobody decided within ``BACKLOG_AFTER`` seconds of that
    stop's own notice. Eligibility is read first, so an ineligible stop never takes the write lock."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_spend_decision import NOTICE
    rows = conn.execute("SELECT DISTINCT t.id FROM tasks t JOIN task_events e ON e.task_id = t.id "
                        "WHERE t.status = 'triage' AND e.kind = ? AND e.created_at <= ?",
                        (NOTICE, now - BACKLOG_AFTER)).fetchall()
    queued = []
    for (task_id,) in rows:
        episode = None if task_id in skip else _current_episode(conn, task_id)
        noticed = _notice_at(conn, task_id, episode) if episode else None
        if noticed is None or noticed > now - BACKLOG_AFTER or not eligible(conn, task_id, episode, cfg):
            continue
        with kb.write_txn(conn):
            if _current_episode(conn, task_id) == episode and queue(conn, task_id, episode, backlog=True, cfg=cfg):
                queued.append(task_id)
    return queued


def _due(conn: sqlite3.Connection, now: int, skip) -> list[tuple[str, str, int]]:
    rows = conn.execute("SELECT DISTINCT e.task_id FROM task_events e JOIN tasks t ON t.id = e.task_id "
                        "WHERE e.kind = ? AND t.status = 'triage'", (QUEUED,)).fetchall()
    due = []
    for (task_id,) in rows:
        episode = _current_episode(conn, task_id)
        if task_id in skip or episode is None:
            continue
        events = _review_events(conn, task_id, episode)
        if not events or events[-1]["kind"] not in (QUEUED, STARTED):
            continue
        last = events[-1]
        if last["kind"] == STARTED and int(last["payload"].get("deadline") or 0) > now:
            continue
        due.append((task_id, episode, sum(1 for e in events if e["kind"] == STARTED)))
    return due


def start_due(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[str]:
    """Queue the backlog and start every due review this process has room for. Returns started task ids."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_ids
    cfg = settings()
    if not cfg[0]:
        return []
    now = int(time.time()) if now is None else int(now)
    skip = foreign_ids(conn)
    queue_backlog(conn, now=now, cfg=cfg, skip=skip)
    _THREADS[:] = [t for t in _THREADS if t.is_alive()]  # a long-lived dispatcher keeps only running reviews
    db, started = _db_path(conn), []
    for task_id, episode, attempts in _due(conn, now, skip):
        if attempts >= MAX_ATTEMPTS:
            escalate(conn, task_id, episode, f"the spend reviewer did not finish after {attempts} attempts")
            continue
        with _LOCK:
            if len(_INFLIGHT) >= MAX_CONCURRENT:
                break
            if (db, task_id) in _INFLIGHT:
                continue
            _INFLIGHT.add((db, task_id))
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, STARTED, {"episode": episode, "attempt": attempts + 1,
                                                      "deadline": now + LEASE_SECONDS})
        thread = threading.Thread(target=contextvars.copy_context().run, args=(_run, db, task_id, episode),
                                  name=f"spend-review-{task_id}", daemon=True)
        thread.start()
        _THREADS.append(thread)
        started.append(task_id)
    return started


def wait_for_reviews(timeout: float = TIMEOUT + 30) -> None:
    """A one-shot dispatch process waits for the reviews it started; its exit would kill their threads."""
    deadline = time.monotonic() + timeout
    while _THREADS:
        thread = _THREADS.pop()
        thread.join(max(0.0, deadline - time.monotonic()))


def _run(db: str, task_id: str, episode: str) -> None:
    from pathlib import Path
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_hub_admission import _owner_scope
    try:
        with _owner_scope(), kbc.connect_closing(Path(db)) as conn:
            review(conn, task_id, episode)
    except Exception:  # health: allow BLE001 -- the lease expires and the next tick retries or escalates
        logger.exception("kanban: spend review of %s failed; the lease will retry it", task_id)
    finally:
        with _LOCK:
            _INFLIGHT.discard((db, task_id))


# --- the review ----------------------------------------------------------------------------------------------------

def _clip(value: Any, limit: int = _TEXT_MAX) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


_SYSTEM_AUTHORS = frozenset({AUTHOR, "kanban-spend-budget"})


def context(conn: sqlite3.Connection, task_id: str, episode: str) -> Optional[dict]:
    """What the judge reads, and the window text a progress quote must come from."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    task, stop = kb.get_task(conn, task_id), decision.pending_episode(conn, task_id)
    if task is None or episode_of(stop) != episode:
        return None
    payload = _payload(stop["payload"])
    since = int(payload.get("since") or 0)
    runs = [{"outcome": r[0], "summary": _clip(r[1])} for r in conn.execute(
        "SELECT outcome, summary FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY id DESC LIMIT 8",
        (task_id, since)).fetchall()][::-1]
    comments = [{"author": r[0], "body": _clip(r[1]), "in_window": r[2] >= since} for r in conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 12",
        (task_id,)).fetchall()][::-1]
    corpus = [r["summary"] for r in runs] + [c["body"] for c in comments
                                               if c["in_window"] and c["author"] not in _SYSTEM_AUTHORS]
    allowance = int((payload.get("limits") or {}).get("runs") or 0)
    return {"task_id": task_id, "title": task.title, "body": _clip(task.body, 4000),
            "stop": {"limit": payload.get("limit"), "runs_spent": payload.get("runs"), "allowance": allowance,
                     "reason": payload.get("reason")},
            "runs": runs, "comments": comments, "_corpus": _norm("\n".join(corpus)), "_cap": _cap(allowance)}


def _cap(allowance: int) -> int:
    from hermes_cli.kanban_spend_promote import MAX_RUNS
    from hermes_cli.kanban_spend_budget import DEFAULT_RUNS
    return max(1, min(allowance or DEFAULT_RUNS, MAX_RUNS))


PROMPT = """You review a Kanban card whose spend budget just ran out. An agent works the card in runs; the budget
stops it after a set number of runs so a looping card cannot burn money. Decide whether it gets more runs now.

Grant when the stopped window shows concrete progress toward the card's goal (a fix landed or a PR moved, new
evidence was gathered, a phase finished, a reviewed plan is being followed) and the next step is clear and bounded.
Waiting for something external (CI, a deploy, a review) is fine when the card parks between checks.
Escalate when the runs repeat the same state without new evidence, loop on the same error, keep asking the same
question, or the work looks out of scope, unsafe or pointless; the owner then decides.

Answer with JSON only:
{"decision": "grant" | "escalate",
 "runs": <integer 1..CAP, the runs the next step needs; omit when escalating>,
 "progress_quote": "<when granting: at least 30 characters copied verbatim from a run summary or comment in
                    this window that shows the progress>",
 "reason": "<one or two plain sentences for the card's owner>"}"""


def judge(ctx: dict) -> Optional[dict]:
    """One subscription call bounded end to end by ``TIMEOUT``, route and credential resolution included.
    None when it is unavailable, late, routed anywhere but the subscription judge, or not JSON."""
    from agent import auxiliary_client as aux
    from hermes_cli.kanban_hub_admission import _bounded, _owner_scope
    user = {k: v for k, v in ctx.items() if not k.startswith("_")}
    route: dict = {}
    deadline = time.monotonic() + TIMEOUT

    def ask():
        with aux.aux_stream_deadline(deadline):
            return aux.call_llm(
                task="kanban_spend_review", provider=PROVIDER, model=MODEL, timeout=TIMEOUT, max_tokens=800,
                temperature=0, route_info=route,
                messages=[{"role": "system", "content": PROMPT.replace("CAP", str(ctx["_cap"]))},
                          {"role": "user", "content": json.dumps(user, ensure_ascii=False)}])

    with _owner_scope():
        response = _bounded(ask, deadline)
    if response is None:
        logger.warning("kanban: spend review judge did not answer within %ss; escalating", TIMEOUT)
        return None
    if (route.get("provider"), route.get("model"), route.get("billing_group")) != (PROVIDER, MODEL, "subscription"):
        logger.warning("kanban: spend review route %s is not the subscription judge; escalating", route)
        return None
    return parse(response.choices[0].message.content)


def parse(content: Any) -> Optional[dict]:
    match = re.search(r"\{[\s\S]*\}", str(content or ""))
    data = _payload(match.group(0)) if match else {}
    return data if data.get("decision") in ("grant", "escalate") else None


def check_grant(verdict: dict, ctx: dict) -> tuple[Optional[int], str]:
    """(runs, '') for a grant code accepts, else (None, why it escalates)."""
    runs = verdict.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= ctx["_cap"]:
        return None, f"it asked for {runs!r} runs, outside 1..{ctx['_cap']}"
    quote = _norm(str(verdict.get("progress_quote") or ""))
    if len(quote) < QUOTE_MIN or quote not in ctx["_corpus"]:
        return None, "it cited no verbatim progress from this window's runs or comments"
    if not str(verdict.get("reason") or "").strip():
        return None, "it gave no reason"
    return runs, ""


def review(conn: sqlite3.Connection, task_id: str, episode: str) -> str:
    """Judge one stop and apply the verdict. Returns ``granted``, ``escalated`` or ``moot``."""
    ctx = context(conn, task_id, episode)
    if ctx is None or state(conn, task_id, episode) != STARTED:
        return "moot"
    try:
        verdict = judge(ctx)
    except Exception as exc:  # health: allow BLE001 -- an unavailable judge escalates, never grants
        logger.warning("kanban: spend review judge failed for %s: %s", task_id, exc, exc_info=True)
        verdict = None
    if verdict is None:
        return escalate(conn, task_id, episode, "the reviewer model was unavailable")
    if verdict["decision"] == "grant":
        runs, why = check_grant(verdict, ctx)
        if runs is not None:
            refusal = grant(conn, task_id, episode, runs, str(verdict["reason"]).strip(),
                            str(verdict.get("progress_quote")))
            if refusal is None:
                return "granted"
            why = refusal
        return escalate(conn, task_id, episode, f"the reviewer's grant was refused: {why}",
                        recommendation=str(verdict.get("reason") or ""))
    return escalate(conn, task_id, episode, str(verdict.get("reason") or "the reviewer escalated"))


def _refusal(conn: sqlite3.Connection, task_id: str, episode: str) -> Optional[str]:
    """Inside the write transaction: why this stop can no longer be granted by the reviewer."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    from hermes_cli.kanban_sync_divergence import _event_order
    row = conn.execute("SELECT status, human_request, block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    stop = decision.pending_episode(conn, task_id)
    if row is None or row[0] != "triage" or episode_of(stop) != episode or state(conn, task_id, episode) != STARTED:
        return "the stop was decided or changed while the reviewer read it"
    if not settings()[0]:
        return "the reviewer was turned off while it read the stop"
    if row[1] or row[2] in HUMAN_BLOCK_KINDS:
        return "the card waits on a human"
    if decision.execution_restricted(conn, task_id) or decision.execution_refusal(conn, task_id):
        return "the stop is a scope or safety stop"
    if _disposed(conn, task_id, episode):
        return "a closed decision card already disposed of the stop"
    after = _event_order(stop)
    for ev in conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = 'commented' AND json_valid(payload) "
                           "AND (json_extract(payload, '$.writer_kind') = 'human' "
                           "OR json_extract(payload, '$.relayed_human') IS NOT NULL)", (task_id,)):
        if _event_order(dict(ev)) > after:
            return "a human commented after the stop"
    if [p for p, st in kb.parent_states(conn, task_id) if st not in ("done", "archived")]:
        return "the card has open parents"
    return None


def grant(conn: sqlite3.Connection, task_id: str, episode: str, runs: int, reason: str, quote: str) -> Optional[str]:
    """Promote with a fresh window of exactly ``runs`` runs, as the decision card's promotion does."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli.kanban_spend_promote import WINDOW_KEY
    with kb.write_txn(conn):
        if why := _refusal(conn, task_id, episode):
            return why
        prior = ksb.card_override(conn, task_id) or {}
        if conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'triage'",
                        (task_id,)).rowcount != 1:
            raise RuntimeError(f"{task_id} left triage inside the spend review transaction")
        kb._append_event(conn, task_id, ksb.OVERRIDE_EVENT, {
            "runs": runs, "cache_read_tokens": ksb._limit_value(prior.get("cache_read_tokens")),
            "by": AUTHOR, "reason": reason, "episode": episode})
        kb._append_event(conn, task_id, "promoted_manual", {
            "actor": AUTHOR, "reason": reason, "from_status": "triage",
            WINDOW_KEY: {"runs": runs, "episode": episode, "decision": "spend_review"}})
        kb._append_event(conn, task_id, GRANTED, {"episode": episode, "runs": runs, "reason": reason,
                                                  "progress_quote": _clip(quote, 400)})
        kb.add_comment(conn, task_id, AUTHOR,
                       f"Spend reviewer granted {runs} more run(s) in a fresh window (stop {episode}): {reason}\n"
                       f"Progress it cited: \"{_clip(quote, 400)}\"", system=True)
    _close_decision_card(conn, task_id, episode, runs)
    return None


def _close_decision_card(conn: sqlite3.Connection, task_id: str, episode: str, runs: int) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_spend_decision import episode_route
    route = episode_route(conn, task_id, episode)
    if not route or route.get("kind") != "supervisor":
        return
    try:
        kb.archive_task(conn, route["task_id"], comment=(
            AUTHOR, f"Decided by the spend reviewer: {task_id} was granted {runs} more run(s) for stop {episode}."))
    except Exception:  # health: allow BLE001 -- the grant stands; the open decision card finds its source promoted
        logger.warning("kanban: spend reviewer granted %s but could not archive decision card %s",
                       task_id, route["task_id"], exc_info=True)


def escalate(conn: sqlite3.Connection, task_id: str, episode: str, reason: str, *, recommendation: str = "") -> str:
    """Record why the reviewer did not grant, then route the stop the usual way (chat or decision card)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    with kb.write_txn(conn):
        if _current_episode(conn, task_id) != episode or state(conn, task_id, episode) not in (QUEUED, STARTED):
            return "moot"
        kb._append_event(conn, task_id, ESCALATED, {"episode": episode, "reason": _clip(reason, 600),
                                                    "recommendation": _clip(recommendation, 600)})
        note = f"Spend reviewer did not grant more runs (stop {episode}): {_clip(reason, 600)}"
        if recommendation and recommendation != reason:
            note += f"\nIts read: {_clip(recommendation, 600)}"
        kb.add_comment(conn, task_id, AUTHOR, note + "\nThe owner decides: raise the budget and promote, "
                       "re-scope, or archive.", system=True)
    decision.safe_ensure_producer(conn, task_id)
    return "escalated"
