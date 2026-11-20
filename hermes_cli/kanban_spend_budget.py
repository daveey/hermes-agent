"""Cumulative spend budget: a card that keeps spending without handing work on goes to triage. [daveey]

The no-progress budget (``kanban_no_progress``) and the continuation cap count *consecutive* idle outcomes,
and a card that alternates (continue, block, schedule, reclaim, continue ...) or changes something small on
every run never trips them. In the 7 days to 2026-11-14 the hub's default board spent 7,812 runs on 2,170
completions; cards with >= 10 runs in that week accounted for 24% of all fleet sessions (t_01a1a1dea9c64d6a
85 runs and still ``todo``, t_01a1a80b7d6cc910 73, t_7d17e13d 57).

This budget counts *spending runs* since the card last handed work on (a run that ended ``completed``,
``review_requested`` or ``changes_requested``), since a human answered on it (a ``commented`` event typed by a
person, ``writer_kind='human'``, or a chat's verified relay of one, ``relayed_human``: the no-progress budget's
rule, so answering a card that spent its budget while blocked gets it a run) or since this budget last sent it
to triage, and adds up
the tokens those runs reported. A spending run is a closed run that lasted at least ``MIN_RUN_SECONDS``:
quota walls, released claims and spawn failures end in seconds and spend nothing. Tokens come from the
run's own ``worker_result`` receipt (``usage``, written by the managed worker from its agent's session
counters on the host that ran it, so a card that ran on several workers is still summed on its home board);
a run with no usage receipt counts as a run only. Runs in which the profile owner refused the claim and no
turn ran (``owner_unavailable`` / ``owner_down``, or ``crashed`` with the owner-unreachable exit 69 and no usage
receipt) are not spending runs; ``Spend.excluded`` counts them.

At ``kanban.spend_budget_runs`` spending runs (default 15) or ``kanban.spend_budget_cache_read_tokens``
cache-read tokens (default 30M) -- 0 disables either -- the dispatcher does not spawn the card. Deciding
again under the write lock and never under another dispatcher's live claim, it moves the card to
``triage`` with a ``spend_budget_triaged`` event and a comment showing the spend. Triage is a hold: the
auto-decomposer skips cards that already ran, so the card waits for a person (or an orchestrator) to
split it, re-scope it, archive it or promote it; a promote starts a new budget window.

A card can carry its own limits (``set_override``, ``hermes kanban spend-budget <id>``): a ``spend_budget_set``
event naming who set it and why, newest wins, 0 = no limit, an unset field keeps the board's. Standing cards
meant to run indefinitely (a shepherd) are exempted this way, explicitly and auditable, never by title.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

DEFAULT_RUNS = 15
DEFAULT_CACHE_READ = 30_000_000
MIN_RUN_SECONDS = 60
EVENT = "spend_budget_triaged"
OVERRIDE_EVENT = "spend_budget_set"  # a card's own limits (``set_override``); the newest one wins
AUTHOR = "kanban-spend-budget"
# A run that handed work on closes the budget window.
RESET_OUTCOMES = ("completed", "review_requested", "changes_requested")
# Runs booked while the profile owner refused the claim ran no turn and spent nothing. ``owner_unavailable`` /
# ``owner_down`` are booked as such; the fifth consecutive refusal is booked ``crashed`` with the worker's
# owner-unreachable exit code (``kanban_owner_unavailable``: 69) and no usage receipt.
REFUSAL_OUTCOMES = ("owner_unavailable", "owner_down")
REFUSAL_EXIT_CODE = 69
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_AGENT_COUNTERS = {
    "input_tokens": "session_input_tokens", "output_tokens": "session_output_tokens",
    "cache_read_tokens": "session_cache_read_tokens", "cache_write_tokens": "session_cache_write_tokens",
}


class Limits(NamedTuple):
    runs: int
    cache_read_tokens: int


class Spend(NamedTuple):
    runs: int  # spending runs in the window
    since: int  # epoch the window opened (0 = the card's first run)
    tokens: dict  # summed USAGE_KEYS over the runs that reported usage
    reported: int  # how many of ``runs`` carried a usage receipt
    cost_usd: float
    excluded: int = 0  # closed runs left out because the owner refused them and no turn ran


def _int(raw: Any, default: int) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def limits(cfg: Optional[dict] = None) -> Limits:
    """``kanban.spend_budget_runs`` / ``kanban.spend_budget_cache_read_tokens``; 0 disables that limit."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:  # health: allow BLE001 -- the dispatcher must not die on an unreadable config
            logger.warning("kanban: spend budget could not read config; using defaults", exc_info=True)
            cfg = {}
    section = (cfg or {}).get("kanban") or {}
    return Limits(_int(section.get("spend_budget_runs", DEFAULT_RUNS), DEFAULT_RUNS),
                  _int(section.get("spend_budget_cache_read_tokens", DEFAULT_CACHE_READ), DEFAULT_CACHE_READ))


def _limit_value(raw: Any) -> Optional[int]:
    """A per-card limit: a non-negative int (0 = no limit), else None (use the board's)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def card_override(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's newest ``spend_budget_set`` payload, or None. A malformed payload reads as no override."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                       (task_id, OVERRIDE_EVENT)).fetchone()
    try:
        data = json.loads(row[0] or "{}") if row else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def card_limits(conn: sqlite3.Connection, task_id: str, lim: Limits) -> Limits:
    """``lim`` with the card's own override applied field by field (an unset field keeps the board's)."""
    data = card_override(conn, task_id) or {}
    runs, cache = _limit_value(data.get("runs")), _limit_value(data.get("cache_read_tokens"))
    return Limits(lim.runs if runs is None else runs, lim.cache_read_tokens if cache is None else cache)


def override_set(data: Optional[dict]) -> bool:
    """Whether an override payload sets any limit (a cleared or malformed one does not)."""
    return any(_limit_value((data or {}).get(key)) is not None for key in ("runs", "cache_read_tokens"))


def _has_override(conn: sqlite3.Connection, task_id: str) -> bool:
    return override_set(card_override(conn, task_id))


def set_override(conn: sqlite3.Connection, task_id: str, *, runs: Optional[int] = None,
                 cache_read_tokens: Optional[int] = None, by: str, reason: str) -> dict:
    """Give ``task_id`` its own limits (0 = no limit; None = the board's), recorded as a ``spend_budget_set``
    event and a comment naming ``by`` and ``reason``. Both None clears the override. The newest event wins.
    For cards meant to run indefinitely (standing shepherds), set explicitly, never inferred from a title."""
    from hermes_cli import kanban_db as kb

    for name, value in (("runs", runs), ("cache_read_tokens", cache_read_tokens)):
        if value is not None and _limit_value(value) is None:
            raise ValueError(f"spend budget {name} must be a non-negative integer (0 = no limit), not {value!r}")
    by, reason = str(by or "").strip(), str(reason or "").strip()
    if not by or not reason:
        raise ValueError("a spend budget override needs who set it (by) and why (reason)")
    payload = {"runs": runs, "cache_read_tokens": cache_read_tokens, "by": by, "reason": reason}
    with kb.write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise ValueError(f"unknown task {task_id}")
        kb._append_event(conn, task_id, OVERRIDE_EVENT, payload)
        kb.add_comment(conn, task_id, AUTHOR, f"Spend budget for this card: {_override_text(payload)}, set by {by}: "
                       f"{reason}.", system=True)
    return payload


def _show_limit(value: Optional[int], text: str) -> str:
    return "the board's" if value is None else ("no limit" if value == 0 else text)


def _override_text(data: dict) -> str:
    runs, cache = _limit_value(data.get("runs")), _limit_value(data.get("cache_read_tokens"))
    if runs is None and cache is None:
        return "board limits (override cleared)"
    if runs == 0 and cache == 0:
        return "no spend budget (exempt)"
    return (f"runs {_show_limit(runs, str(runs))}, "
            f"cache-read tokens {_show_limit(cache, _m(cache) if cache else '')}")


def usage_fields(agent) -> dict:
    """``{'usage': {...}}`` for a managed worker's ``worker_result`` receipt: the agent's session token
    counters (and estimated cost) when it has any, else ``{}``. Never raises."""
    if agent is None:
        return {}
    usage: dict[str, Any] = {}
    for key, attr in _AGENT_COUNTERS.items():
        value = getattr(agent, attr, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            usage[key] = int(value)
    cost = getattr(agent, "session_estimated_cost_usd", 0)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        usage["cost_usd"] = round(float(cost), 4)
    return {"usage": usage} if usage else {}


def _window_start(conn: sqlite3.Connection, task_id: str, cut: int) -> int:
    marks = ",".join("?" for _ in RESET_OUTCOMES)
    handed = conn.execute(
        f"SELECT MAX(ended_at) FROM task_runs WHERE task_id = ? AND outcome IN ({marks}) AND ended_at < ?",
        (task_id, *RESET_OUTCOMES, cut)).fetchone()[0]
    triaged = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = ? AND created_at < ?",
                           (task_id, EVENT, cut)).fetchone()[0]
    # A human answer (typed by a person, or a chat's verified relay of one -- the no-progress budget's rule)
    # re-authorizes the card: without it, answering a card that spent its budget while blocked sent it straight
    # to triage with no run. Agent comments and bare ``unblocked`` events carry no human and do not count.
    answered = conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'commented' AND created_at < ? "
        "AND (json_extract(payload, '$.writer_kind') = 'human' OR json_extract(payload, '$.relayed_human') IS NOT NULL)",
        (task_id, cut)).fetchone()[0]
    # A spend decision applied through the operator courier (``kanban_spend_promote``) opens the new window at the
    # promotion itself, so its run budget counts exactly the runs after it. A plain manual promote does not.
    promoted = conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'promoted_manual' AND created_at < ? "
        "AND json_valid(payload) AND json_extract(payload, '$.spend_budget.runs') IS NOT NULL",
        (task_id, cut)).fetchone()[0]
    return int(max(handed or 0, triaged or 0, answered or 0, promoted or 0))


def _run_usage(conn: sqlite3.Connection, task_id: str, run_ids: list[int]) -> dict[int, dict]:
    """``run_id -> usage`` from each run's newest ``worker_result`` that carried one."""
    out: dict[int, dict] = {}
    if not run_ids:
        return out
    marks = ",".join("?" for _ in run_ids)
    for run_id, raw in conn.execute(
            f"SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'worker_result' "
            f"AND run_id IN ({marks}) AND json_extract(payload, '$.usage') IS NOT NULL ORDER BY id",
            (task_id, *run_ids)):
        try:
            usage = (json.loads(raw or "{}") or {}).get("usage")
        except (ValueError, AttributeError):
            continue
        if isinstance(usage, dict):
            out[int(run_id)] = usage  # the newest receipt for a run wins
    return out


def _refused(outcome: Any, raw_metadata: Any, has_usage: bool) -> bool:
    """True for a run in which the owner refused the claim and no turn ran."""
    if outcome in REFUSAL_OUTCOMES:
        return True
    if outcome != "crashed" or has_usage:
        return False
    try:
        meta = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    except ValueError:
        return False
    code = meta.get("exit_code") if isinstance(meta, dict) else None
    return code == REFUSAL_EXIT_CODE and not isinstance(code, bool)


def spend(conn: sqlite3.Connection, task_id: str, *, as_of: Optional[int] = None) -> Spend:
    """What the card spent since it last handed work on (or was last sent to triage by this budget)."""
    cut = int(time.time()) + 1 if as_of is None else int(as_of)
    since = _window_start(conn, task_id, cut)
    rows = conn.execute(
        "SELECT id, outcome, metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL AND ended_at < ? "
        "AND started_at >= ? AND ended_at - started_at >= ?", (task_id, cut, since, MIN_RUN_SECONDS)).fetchall()
    usage = _run_usage(conn, task_id, [int(r[0]) for r in rows])
    run_ids = [int(r[0]) for r in rows if not _refused(r[1], r[2], int(r[0]) in usage)]
    excluded = len(rows) - len(run_ids)
    tokens = {key: 0 for key in USAGE_KEYS}
    cost = 0.0
    for item in usage.values():
        for key in USAGE_KEYS:
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                tokens[key] += int(value)
        value = item.get("cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            cost += float(value)
    return Spend(len(run_ids), since, tokens, len(usage), round(cost, 2), excluded)


def over(spent: Spend, lim: Limits) -> Optional[str]:
    """Which limit ``spent`` reached (``runs`` / ``cache_read_tokens``), or None."""
    if lim.runs and spent.runs >= lim.runs:
        return "runs"
    if lim.cache_read_tokens and spent.tokens["cache_read_tokens"] >= lim.cache_read_tokens:
        return "cache_read_tokens"
    return None


def _m(n: int) -> str:
    return f"{n / 1_000_000:.1f}M"


def describe(spent: Spend, lim: Limits, which: str, *, override: bool = False) -> str:
    since = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(spent.since)) if spent.since else "the card's first run"
    t = spent.tokens
    tokens = (f"{_m(t['cache_read_tokens'])} cache-read, {_m(t['input_tokens'])} input, {_m(t['output_tokens'])} output, "
              f"{_m(t['cache_write_tokens'])} cache-write tokens over the {spent.reported} run(s) that reported usage")
    cost = f", ~${spent.cost_usd:.2f} estimated" if spent.cost_usd else ""
    limit = (f"{lim.runs} runs" if which == "runs" else f"{_m(lim.cache_read_tokens)} cache-read tokens")
    source = ("this card's override (`hermes kanban spend-budget`)" if override
              else "kanban.spend_budget_runs / kanban.spend_budget_cache_read_tokens")
    return (f"Spend budget: {spent.runs} runs since {since} without a completion or review hand-off; "
            f"{tokens}{cost}. Limit: {limit} ({source})")


def _comment(task_id: str, note: str) -> str:
    return (f"{note}.\n\nThe dispatcher moved this card to triage instead of spawning another worker. A person "
            "or orchestrator decides: split it into smaller cards, re-scope or re-route it (a cheaper model, a real "
            "wake condition), archive it, or promote it to run again; a promote starts a new budget window. A card "
            "meant to run indefinitely gets its own limit instead: `hermes kanban spend-budget "
            f"{task_id} --exempt --reason ...`. Do not just requeue {task_id} unchanged: the same loop costs the "
            "same again.")


# Never take a card off the lane under a live claim another dispatcher holds.
_UNCLAIMED = "(claim_lock IS NULL OR claim_expires IS NULL OR claim_expires < ?)"


def hold(conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
         lim: Optional[Limits] = None) -> Optional[str]:
    """Called by the dispatcher before claiming a ready/review card. None = spawn as usual; ``triage`` = the card
    was over budget and moved to triage."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_copy

    from hermes_cli import kanban_spend_decision as decision

    if foreign_copy(conn, task_id):
        return None  # a worker-node copy: its home board owns the decision
    now = int(time.time()) if now is None else int(now)
    if decision.pending(conn, task_id):
        with kb.write_txn(conn):
            if decision.pending(conn, task_id):
                cur = conn.execute("UPDATE tasks SET status='triage' WHERE id=? AND status IN ('ready','review') "
                             "AND current_run_id IS NULL AND " + _UNCLAIMED, (task_id, now))
                if cur.rowcount == 1:
                    kb._append_event(conn, task_id, "spend_budget_held", {
                        "status": "triage", "reason": decision.unblock_refusal(conn, task_id)})
                decision.safe_ensure_producer(conn, task_id)
                return "triage"
    board = limits() if lim is None else lim
    lim = card_limits(conn, task_id, board)
    if not lim.runs and not lim.cache_read_tokens:
        return None
    if over(spend(conn, task_id, as_of=now + 1), lim) is None:
        return None  # cheap read on every tick; the write lock is taken only for a card over budget
    from hermes_cli import kanban_spend_pr_owner as owner
    pr = owner.open_core_pr(conn, task_id)  # cached network read, only over budget and outside the write lock
    with kb.write_txn(conn):
        # Decide again under the lock, with a cutoff taken now: a hand-off or an override recorded since the read
        # above must win.
        now = max(now, int(time.time()))
        lim = card_limits(conn, task_id, board)
        override = _has_override(conn, task_id)
        spent = spend(conn, task_id, as_of=now + 1)
        which = over(spent, lim)
        if which is None:
            return None
        if pr is not None and pr in owner.recognised_prs(conn, task_id):
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id=? AND status IN ('ready','review') "
                "AND current_run_id IS NULL AND " + _UNCLAIMED, (task_id, now)).fetchone()
            if row is not None:
                owner.record_spared(conn, task_id, pr, spent, describe(spent, lim, which, override=override))
            return None
        cur = conn.execute(
            "UPDATE tasks SET status = 'triage', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('ready', 'review') AND current_run_id IS NULL AND " + _UNCLAIMED,
            (task_id, now))
        if cur.rowcount != 1:
            return None
        note = describe(spent, lim, which, override=override)
        kb._append_event(conn, task_id, EVENT, {
            "limit": which, "runs": spent.runs, "since": spent.since, "tokens": spent.tokens,
            "runs_with_usage": spent.reported, "cost_usd": spent.cost_usd, "override": override,
            "limits": {"runs": lim.runs, "cache_read_tokens": lim.cache_read_tokens}, "reason": note})
        kb.add_comment(conn, task_id, AUTHOR, _comment(task_id, note), system=True)
        decision.safe_ensure_producer(conn, task_id)
    logger.info("kanban: spend budget sent %s to triage (%s: %d runs, %s cache-read)", task_id, which,
                spent.runs, _m(spent.tokens["cache_read_tokens"]))
    return "triage"
