"""Guarded retirement of moot, automation-filed lane backlog. [daveey]

A held card is archived only when apply itself proves it moot; the planner's prose is never evidence.

======================  ================================================================================
``basis``               proof checked here
======================  ================================================================================
``target_done``         the card the alarm is about (named in the producer's idempotency key) is done/archived
``superseded``          ``by`` names a newer open card from the same producer about the same subject
``condition_cleared``   the producer's read-only check, registered by the operator under
                        ``kanban.admission.archive_checks.<producer>`` (never taken from the plan), says ``clear``
``stale``               an informational producer's card held more than seven days (a daily intake: any earlier day)
``producer_withdrawn``  the producer itself withdraws its own card (``hermes kanban lanes withdraw``)
======================  ================================================================================

Every basis also needs the shared guard: held at a lane gate on the board's home, filed by a catalogued automation
(not a chat session), no human request, no execution custody or live run, no containment and no open child. The
guard runs inside archive_task's writer transaction, so an earlier snapshot cannot authorize retiring a card which
acquired a worker, ask or dependent meanwhile. A condition check runs before that lock and is re-bound to the same
idempotency key inside it.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_lanes as lanes

STALE_SECONDS = 7 * 86400
CHECK_TIMEOUT_S = 60
BASES = ("target_done", "superseded", "condition_cleared", "stale", "producer_withdrawn")
_OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")
_TASK = r"t_[0-9a-f]{8,32}"


@dataclass(frozen=True)
class Producer:
    name: str
    created_by: str
    key: re.Pattern
    informational: bool = False


def _p(name, created_by, key, informational=False):
    return Producer(name, created_by, re.compile(key), informational)


# Each automation is identified by its creator label AND its own idempotency-key shape: a bare non-human label is
# not proof that a card is an expiring report rather than repair work. Named groups: ``subject`` (what a newer card
# about the same thing shares), ``target`` (the card an alarm is about), ``day`` (a daily intake's date).
PRODUCERS = (
    # A per-merge sha (legacy) or one rolling retro card per repo per UTC day (``:<n>`` when that day's card was
    # already done); either expires only after 7 days held.
    _p("core-ship-backstop", "core-ship-backstop",
       r"^(?:[0-9a-f]{40}|core-ship-backstop:[^:\s]+/[^:\s]+:\d{4}-\d{2}-\d{2}(?::\d+)?)$", informational=True),
    _p("model-capacity-watch", "model-capacity-watch", r"^model-capacity-pace:(?P<subject>[^:]+):\d+$",
       informational=True),
    _p("coach-ask-gate", "coach", r"^ask-gate-weekly:\d{4}-\d{2}-\d{2}$", informational=True),
    _p("ci-flaky-intake", "gw:hub", r"^ci-flaky-intake:(?P<subject>[^:]+):(?P<day>\d{4}-\d{2}-\d{2}):",
       informational=True),
    _p("ci-main-watch", "ci-main-watch", r"^ci-red:(?P<subject>.+):[0-9a-f]{40}(?:#\d+)?$"),
    _p("worker-core-rollout", "worker-core-rollout", r"^worker-core-rollout:(?P<subject>[a-z-]+:[^:]+):"),
    _p("self-heal", "self-heal", r"^selfheal:(?P<subject>[^:]+)$"),
    _p("self-heal-backlog", "self-heal", r"^selfheal-backlog:(?P<subject>[^:]+):\d{4}-\d{2}-\d{2}$"),
    _p("scope-renewal-alarm", "scope-renewal-alarm", rf"^scope-renewal-loop:(?P<target>{_TASK}):"),
    _p("spend-stop-watch", "spend-stop-watch", rf"^spend-stop-watch:(?P<target>{_TASK}):"),
    _p("guard-hold", "kanban-dispatcher", rf"^guard-hold:(?P<target>{_TASK})$"),
    _p("pickup-stall", "pickup-stall-detector", rf"^pickup-stall:[^:]+:(?P<target>{_TASK}):"),
)


def producer_of(row) -> tuple[Optional[Producer], Optional[re.Match]]:
    for prod in PRODUCERS:
        if row["created_by"] == prod.created_by:
            match = prod.key.search(row["idempotency_key"] or "")
            if match:
                return prod, match
    return None, None


def _chat_filed(conn, task_id) -> bool:
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
                       (task_id,)).fetchone()
    try:
        payload = json.loads(row[0] or "{}") if row else {}
    except ValueError:
        return True  # unreadable provenance is not proof of an automation
    if not isinstance(payload, dict):
        return True
    source = payload.get("creator_source")
    if source is None:
        return False
    return not isinstance(source, dict) or bool(source.get("session_id"))


def _home_board(conn) -> bool:
    """Children and claims are authoritative only on the board's home; a worker's copy may lack shadow rows."""
    from hermes_cli.kanban_db_sync import sync_state
    state = sync_state(conn)
    return state is None or state.get("gateway_id") in ("hub", "hermes-hub")


def guard(conn, task_id):
    """The checks every basis shares: ``(refusal, producer, key match, row)``."""
    from hermes_cli.kanban_containment_guard import pending

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or not conn.execute(f"SELECT 1 FROM tasks WHERE id=? AND {lanes.HELD}", (task_id,)).fetchone():
        return "card is not held at a lane gate", None, None, row
    if not _home_board(conn):
        return "archive runs only on the board's home, where children and claims are authoritative", None, None, row
    prod, match = producer_of(row)
    if prod is None:
        return "card is not from a catalogued automation", None, None, row
    if _chat_filed(conn, task_id):
        return "card was filed from a chat session", None, None, row
    if row["human_request"]:
        return "card has a human request", prod, match, row
    if row["claim_lock"] or row["worker_pid"] or row["current_run_id"]:
        return "card has execution custody", prod, match, row
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,)).fetchone():
        return "card has a live run", prod, match, row
    if pending(conn, task_id):
        return "card has a containment restriction", prod, match, row
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks t ON t.id=l.child_id "
                    "WHERE l.parent_id=? AND t.status NOT IN ('done','archived')", (task_id,)).fetchone():
        return "card has children waiting on it", prod, match, row
    return None, prod, match, row


def _stale(conn, row, prod, match, now) -> Optional[str]:
    if not prod.informational:
        return f"{prod.name} is not an informational producer; stale applies only to reports"
    if "day" in match.re.groupindex:
        today = _dt.datetime.fromtimestamp(now, _dt.UTC).date().isoformat()
        return None if match["day"] < today else "daily intake is from today"
    gate = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind=?",
                        (row["id"], lanes.GATED)).fetchone()[0]
    if gate is None or max(row["created_at"], gate) >= now - STALE_SECONDS:
        return "informational card has not been held for more than seven days"
    return None


def _target_done(conn, match) -> Optional[str]:
    if "target" not in match.re.groupindex:
        return "this producer's cards name no target card"
    target = conn.execute("SELECT status FROM tasks WHERE id=?", (match["target"],)).fetchone()
    if target is None:
        return f"target card {match['target']} is not on this board"
    if target[0] not in ("done", "archived"):
        return f"target card {match['target']} is still {target[0]}"
    return None


def _superseded(conn, row, prod, match, by) -> Optional[str]:
    if "subject" not in match.re.groupindex:
        return "this producer's cards carry no subject to supersede"
    newer = conn.execute("SELECT * FROM tasks WHERE id=?", (by,)).fetchone() if isinstance(by, str) else None
    if newer is None or newer["id"] == row["id"]:
        return "superseded needs 'by': another card"
    other, other_match = producer_of(newer)
    if other is not prod or other_match["subject"] != match["subject"]:
        return "the newer card is not from the same producer about the same subject"
    if newer["status"] not in _OPEN:
        return "the newer card is not open"
    if (newer["created_at"], newer["id"]) <= (row["created_at"], row["id"]):
        return "the 'by' card is not newer"
    return None


def checker(prod) -> Optional[list]:
    """Operator-configured argv for a producer's read-only condition check (never taken from a plan)."""
    try:
        from hermes_cli.config import load_config
        checks = (((load_config().get("kanban") or {}).get("admission") or {}).get("archive_checks") or {})
        argv = checks.get(prod.name) if isinstance(checks, dict) else None
    except Exception:  # health: allow BLE001 -- an unreadable config registers no check (fail closed)
        return None
    return list(argv) if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) else None


def run_check(prod, row) -> tuple[str, str]:
    """``(state, evidence)``: ``clear``, ``active`` or ``unknown``; anything unexpected is unknown (fail closed)."""
    argv = checker(prod)
    if argv is None:
        return "unknown", f"no condition check is registered for {prod.name}"
    from hermes_cli.worker_drain_fence import DrainRefused, admit, lease_scope
    try:
        lease = admit("kanban.lanes.archive_check")
    except DrainRefused as exc:  # a draining node runs no new checks: unknown, so nothing is archived
        return "unknown", f"condition check refused by worker drain: {exc}"
    try:
        with lease_scope(lease):
            done = subprocess.run([*argv, row["id"], row["idempotency_key"]], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=CHECK_TIMEOUT_S, check=False)
        doc = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return "unknown", f"condition check failed: {type(exc).__name__}"
    finally:
        if lease is not None:
            lease.close()
    if not isinstance(doc, dict):
        return "unknown", "condition check printed no JSON object"
    state, evidence = doc.get("state"), str(doc.get("evidence") or "")[:500]
    if done.returncode != 0 or state not in ("clear", "active"):
        return "unknown", evidence or f"condition check exited {done.returncode}"
    return state, evidence


def refusal(conn, task_id, action, *, now=None, checked=None) -> Optional[str]:
    """Why ``action`` may not archive ``task_id`` now, or None. ``checked``: this card's prior run_check."""
    why, prod, match, row = guard(conn, task_id)
    if why:
        return why
    basis = action.get("basis")
    now = time.time() if now is None else now
    if basis == "stale":
        return _stale(conn, row, prod, match, now)
    if basis == "target_done":
        return _target_done(conn, match)
    if basis == "superseded":
        return _superseded(conn, row, prod, match, action.get("by"))
    if basis == "condition_cleared":
        if not checked or checked.get("key") != row["idempotency_key"]:
            return "the condition was not checked for this card"
        return None if checked["state"] == "clear" else f"condition is {checked['state']}: {checked['evidence']}"
    if basis == "producer_withdrawn":
        return None if action.get("producer") == prod.name else "only the producer that filed the card withdraws it"
    return f"basis must be one of {', '.join(BASES)}"


def _precheck(conn, task_id, action):
    """Run the (possibly network-bound) condition check outside the write lock; other bases need none."""
    if action.get("basis") != "condition_cleared":
        return None, None
    why, prod, _match, row = guard(conn, task_id)
    if why:
        return why, None
    state, evidence = run_check(prod, row)
    return None, {"key": row["idempotency_key"], "state": state, "evidence": evidence}


def check(conn, action, row, *, now=None) -> Optional[str]:
    """Read-only verdict for a dry run: the same proof apply would require, without writing."""
    if action.get("basis") == "producer_withdrawn":
        return "producer_withdrawn is the producer's own command (lanes withdraw), not a plan basis"
    why, checked = _precheck(conn, row["id"], action)
    return why or refusal(conn, row["id"], action, now=now, checked=checked)


def archive(conn, action, row, *, via, now=None) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    why, checked = _precheck(conn, row["id"], action)
    if why:
        return why
    reasons = []

    def precondition(current):
        refused = refusal(current, row["id"], action, now=now, checked=checked)
        if refused:
            reasons.append(refused)
        return refused

    extra = {"basis": action.get("basis"), "reason": action["reason"]}
    if action.get("by"):
        extra["by"] = action["by"]
    if checked:
        extra["condition"] = {"state": checked["state"], "evidence": checked["evidence"]}
    if kb.archive_task(conn, row["id"], via=via, precondition=precondition, event_extra=extra):
        return None
    return reasons[0] if reasons else "archive did not commit"


def plan_archive(conn, _pol, action, row, state) -> Optional[str]:
    """The planner's handler: every basis except the producer's own withdrawal."""
    if action.get("basis") == "producer_withdrawn":
        return "producer_withdrawn is the producer's own command (lanes withdraw), not a plan basis"
    return archive(conn, action, row, via="lane-planner", now=state.get("now"))


def withdraw(conn, task_id, *, producer, reason) -> Optional[str]:
    """A producer retires its own held card once its condition clears; same guard as the planner's archive.

    Producers run unscoped. A Kanban worker scoped to a card (``HERMES_KANBAN_TASK``) meets the same archive-scope
    wall as ``hermes kanban archive``: ``--producer`` is only what the caller says, so it never widens that wall.
    """
    from hermes_cli.kanban_archive_scope import worker_archive_scope

    row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return "no such card"
    scoped, _via = worker_archive_scope(conn, task_id)
    if scoped:
        return scoped
    return archive(conn, {"basis": "producer_withdrawn", "producer": producer, "reason": reason}, row,
                   via=f"producer:{producer}")


def hint(row) -> Optional[dict]:
    """What a planner may cite for a held card: its producer and typed subject/target/day, for ``lanes held``."""
    prod, match = producer_of(row)
    if prod is None:
        return None
    out = {"producer": prod.name, "informational": prod.informational}
    for name in ("subject", "target", "day"):
        if name in match.re.groupindex:
            out[name] = match[name]
    return out
