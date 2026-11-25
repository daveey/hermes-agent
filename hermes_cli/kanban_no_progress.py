"""No-progress budget: stop re-spawning a card whose runs change nothing. [daveey]

A card can park (``scheduled``), wait-continue or block, get woken by its clock or an unblock, and spawn a
worker that re-reads the same world, posts a reconciliation comment and parks again. Nothing in that loop is a
failure, so neither the crash breaker nor the continuation cap (which only counts back-to-back ``continued``
runs, and resets on any other outcome) sees it: t_e3e216d4 spent 141 runs, t_7e2ee45c 69 and
t_01a194ca90736cea 32, mostly on clock wakes while the card they waited on, or an external precondition,
had not moved.

The budget counts trailing closed runs that ended in a wait (``scheduled``, ``blocked``, or ``continued`` into
``scheduled``) and started after the card's last *progress marker*: a completion or review hand-off, a link,
attachment, body/assignee/placement/pin edit, a met wake condition, a human comment, or a completion, archive,
review or edit on a linked (parent or child) card. Crashes, reclaims, quota releases and other run losses are
neutral: they neither count nor reset. Agent comments are never progress -- they are what the loop produces --
except a chat's verified relay of the user's reply (``relayed_human`` on the ``commented`` event,
``kanban_relayed_human``): an answered ask is a human answer, whoever typed it onto the card. Neither is a bare
``unblocked`` (t_7e2ee45c's recovery loop was 11 of them); an unblock that resolved
something comes with its evidence (a completed parent, a human comment, an edit). An escalation right after a
chat unblocked a ``needs_input`` block with no marked relay records ``no_progress_relay_suspect``: most likely
David answered and the relay was not marked.

At ``kanban.no_progress_runs`` (default 5; 0 disables) such runs, the dispatcher does not spawn. Instead (decided
again under the write lock, and never under another dispatcher's live claim):

* this episode's latest park carried a wake condition whose probe has not broken since -> it is parked again
  behind that same condition, with the clock wake pushed out by ``REPARK_SECONDS``; only the condition (or
  real progress) brings the next run. Each 24 h fallback that finds nothing new re-parks it again: one
  dispatcher event a day, no worker run;
* no condition to wait on (none declared, or its probe kept breaking) -> ONE owner card is filed (idempotent per episode) and the card waits on it in
  ``todo``; it resumes when the owner completes. An owner card that itself loops goes to ``triage``: no
  owner-of-owner chain, and no ``continuation_exhausted`` / ``goal_unachievable`` event is written, so the
  kernel recovery requester (``kanban_resume_requester``) never opens a recovery loop for it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
REPARK_SECONDS = 24 * 3600
PARKED_EVENT = "no_progress_parked"
ESCALATED_EVENT = "no_progress_escalated"
CREATED_BY = "kanban-no-progress"

# Run outcomes a worker chooses when it found nothing to do yet.
WAIT_OUTCOMES = frozenset({"scheduled", "blocked", "continued"})
# A run that handed work on: the streak ends there.
RESET_OUTCOMES = frozenset({"completed", "review_requested", "changes_requested"})
# Events on the card itself that change what the next run would see.
CARD_PROGRESS_EVENTS = (
    "completed", "review_requested", "changes_requested", "linked", "unlinked", "attached", "edited",
    "condition_met", "reopened", "contract_changed", "contract_waived", "assigned", "moved",
    "model_override_set", "promoted_manual", "shared_blocker_cleared",
)
# Events on a parent or child card that count as that card moving.
LINKED_PROGRESS_EVENTS = ("completed", "archived", "review_requested", "changes_requested", "reopened", "edited")


def limit(cfg: Optional[dict] = None) -> int:
    """``kanban.no_progress_runs``: runs without progress before the dispatcher stops spawning; 0 disables."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:  # health: allow BLE001 -- the dispatcher must not die on an unreadable config
            logger.warning("kanban: no-progress budget could not read config; using %d", DEFAULT_LIMIT, exc_info=True)
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("no_progress_runs", DEFAULT_LIMIT)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _marks(items) -> str:
    return ",".join("?" for _ in items)


def progress_marker(conn: sqlite3.Connection, task_id: str, *, before: Optional[int] = None) -> int:
    """Epoch of the newest progress marker for ``task_id`` (strictly before ``before``, if given), else 0."""
    cut = int(time.time()) + 1 if before is None else int(before)
    own = conn.execute(
        f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND created_at < ? "
        f"AND (kind IN ({_marks(CARD_PROGRESS_EVENTS)}) "
        "OR (kind = 'commented' AND (json_extract(payload, '$.writer_kind') = 'human' "
        "OR json_extract(payload, '$.relayed_human') IS NOT NULL "
        "OR json_extract(payload, '$.recorded_decision.operationId') IS NOT NULL)))",
        (task_id, cut, *CARD_PROGRESS_EVENTS),
    ).fetchone()[0]
    linked = conn.execute(
        f"SELECT MAX(e.created_at) FROM task_events e WHERE e.created_at < ? "
        f"AND e.kind IN ({_marks(LINKED_PROGRESS_EVENTS)}) AND e.task_id IN ("
        "SELECT parent_id FROM task_links WHERE child_id = ? UNION SELECT child_id FROM task_links WHERE parent_id = ?)",
        (cut, *LINKED_PROGRESS_EVENTS, task_id, task_id),
    ).fetchone()[0]
    return int(max(own or 0, linked or 0))


def _run_parks(conn: sqlite3.Connection, task_id: str, cut: int) -> dict[int, dict]:
    """``run_id -> payload`` of the ``scheduled`` / ``continued`` event that ended each of the card's runs."""
    parks: dict[int, dict] = {}
    for run_id, raw in conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled', 'continued') "
            "AND run_id IS NOT NULL AND created_at < ? ORDER BY id", (task_id, cut)):
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            continue
        if isinstance(data, dict):
            parks[int(run_id)] = data  # the newest event for a run wins
    return parks


def _declared_window(park: Optional[dict]) -> bool:
    """A declared pure time window (``clock_only`` or an explicit ``clock`` awaiting): each clock wake is the
    measurement itself, so the run is neutral, not a no-progress run."""
    if not park:
        return False
    awaiting = park.get("awaiting") if isinstance(park.get("awaiting"), dict) else {}
    return bool(park.get("clock_only")) or (awaiting.get("kind") == "clock" and not awaiting.get("inferred"))


# A clock poll (a park with no wake condition: the clock wake is the only way to look again, e.g. a CI wait
# re-checked every 20 min) costs a quarter of a run against the budget, so ``budget * CLOCK_POLL_WEIGHT`` polls
# with nothing changing (20 at the default) end the same way. [daveey]
CLOCK_POLL_WEIGHT = 4


def streak(conn: sqlite3.Connection, task_id: str, *, as_of: Optional[int] = None) -> tuple[float, int]:
    """``(runs, marker)``: trailing waiting runs that started after the last progress marker, a clock poll
    counting ``1 / CLOCK_POLL_WEIGHT``."""
    cut = int(time.time()) + 1 if as_of is None else int(as_of)
    marker = progress_marker(conn, task_id, before=cut)
    units = 0
    parks = _run_parks(conn, task_id, cut)
    for run_id, outcome, started in conn.execute(
            "SELECT id, outcome, started_at FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
            "AND ended_at < ? ORDER BY id DESC", (task_id, cut)):  # stops at the marker; neutral rows never cap it
        if int(started or 0) <= marker or outcome in RESET_OUTCOMES:
            break
        if outcome not in WAIT_OUTCOMES:
            continue  # crash, reclaim, quota release, owner loss: neutral
        park = parks.get(int(run_id)) if outcome != "blocked" else None
        if outcome == "continued" and (park is None or park.get("status") != "scheduled"):
            break  # long work checkpointing itself (an immediate continue) is progress the board cannot see
        if _declared_window(park):
            continue
        has_condition = bool(park and isinstance(park.get("wake_check"), str) and park["wake_check"].strip())
        units += CLOCK_POLL_WEIGHT if outcome == "blocked" or has_condition else 1
    return units / CLOCK_POLL_WEIGHT, marker


def _latest_condition_park(conn: sqlite3.Connection, task_id: str, *, after: int,
                           before: int) -> Optional[tuple[dict, int]]:
    """``(payload, created_at)`` of the newest park (``scheduled`` / ``continued``) of this episode (after the
    progress marker) that waited on a wake condition: a condition from before the last progress is obsolete."""
    for raw, created in conn.execute(
            "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind IN ('scheduled', 'continued') "
            "AND created_at > ? AND created_at < ? ORDER BY id DESC", (task_id, after, before)):
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("wake_check"), str) and data["wake_check"].strip() \
                and not data.get("clock_only"):
            return data, int(created)
    return None


def decide(conn: sqlite3.Connection, task_id: str, *, budget: int,
           as_of: Optional[int] = None) -> Optional[tuple[str, int, int, Optional[dict]]]:
    """``None`` (spawn; always when ``budget`` <= 0) or ``(action, runs, marker, park)``: ``action`` is ``park`` (re-park behind ``park``'s
    wake condition) or ``escalate`` (no usable condition: none was declared in this episode, or that probe
    broke since it was set, so parking behind it would only re-wake the card)."""
    cut = int(time.time()) + 1 if as_of is None else int(as_of)
    runs, marker = streak(conn, task_id, as_of=cut)
    if budget <= 0 or runs < budget:
        return None
    found = _latest_condition_park(conn, task_id, after=marker, before=cut)
    if found is not None:
        park, parked_at = found
        broken = conn.execute(  # that probe (not an earlier one) broke since it was set
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'wake_check_broken' AND created_at >= ? "
            "AND created_at < ? LIMIT 1", (task_id, parked_at, cut)).fetchone()
        if not broken:
            return "park", runs, marker, park
    return "escalate", runs, marker, None


def _note(runs: int, marker: int) -> str:
    since = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(marker)) if marker else "the card's first run"
    return (f"No-progress budget: {runs} runs in a row since {since} ended in a wait with no status, link, "
            "attachment, edit, met condition, human comment or linked-card change")


# Never take a card off the lane under a live claim another dispatcher holds.
_UNCLAIMED = "(claim_lock IS NULL OR claim_expires IS NULL OR claim_expires < ?)"


def _repark(conn, task_id: str, park: dict, runs: int, marker: int, now: int) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    wake_check = park["wake_check"].strip()
    every = kwc.normalize_every(park.get("check_every"))
    host = park.get("probe_host") if isinstance(park.get("probe_host"), str) else None
    own = kwc.own_gateway(conn)
    # A lost remote executor is not a broken condition. Keep the condition, but move
    # its custody home instead of stamping a fresh park that resets the silence clock.
    lost = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND id > "
        "(SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind IN ('scheduled', 'continued')) "
        "ORDER BY id DESC LIMIT 1", (task_id, kwc.LOST_EVENT, task_id)).fetchone()
    recovery = kb._json_dict(lost[0]) if lost else {}
    if (host and recovery.get("reason") == "remote_probe_silent"
            and recovery.get("probe_host") == host and recovery.get("wake_check") == wake_check):
        host = own  # hold() only runs on the card's home; unset on an unsynced board means local.
    reason = (f"{_note(runs, marker)}. Parked behind its own wake condition; only the condition or real "
              f"progress brings the next run (clock fallback in {REPARK_SECONDS // 3600} h).")
    # Runs inside hold()'s write transaction.
    cur = conn.execute(
        "UPDATE tasks SET status = 'scheduled', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
        "WHERE id = ? AND status IN ('ready', 'review') AND current_run_id IS NULL AND " + _UNCLAIMED,
        (task_id, now))
    if cur.rowcount != 1:
        return False
    payload: dict[str, Any] = {"reason": reason, "wake_at": now + REPARK_SECONDS, "wake_check": wake_check,
                               "check_every": every, "no_progress": {"runs": runs, "since": marker}}
    if host:
        payload["probe_host"] = host
    if isinstance(park.get("awaiting"), dict):
        payload["awaiting"] = park["awaiting"]
    kb._append_event(conn, task_id, "scheduled", payload)
    kb._append_event(conn, task_id, PARKED_EVENT, {"runs": runs, "since": marker, "wake_check": wake_check})
    kwc.set_check(conn, task_id, wake_check, every, now=now, register_here=not host or not own or host == own)
    return True


def _owner_body(task, runs: int, marker: int) -> str:
    from hermes_cli.kanban_model_neutral import forwarded_text

    source_title = forwarded_text(task.title)
    return (
        f"{_note(runs, marker)}: card {task.id}. Its latest park named no usable wake condition, so the "
        "dispatcher stopped spawning it and it now waits on this card. (A wake check that kept breaking counts as "
        "no condition: parking behind it only re-woke the card.)\n\n"
        "This is agent-owned, not a question for David. Read its runs and comments (`kanban_show`) and remove the "
        "cause once: do the missing step where the capability lives, give the card a real wake condition "
        "(`kanban_schedule` with `wake_check`), split it, or complete/archive it with a comment naming the "
        f"disposition. Do not unblock or requeue {task.id} unchanged: that restarts the same loop. Complete this "
        f"card with the evidence; {task.id} resumes when it is done.\n\nSource title (evidence):\n{source_title}"
    )


def _escalate(conn, task, runs: int, marker: int, now: int) -> Optional[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_relayed_human as krh
    from hermes_cli.kanban_awaiting import _supervisor_profile

    note = _note(runs, marker)
    # Detection for an unmarked relay (kanban-d1b921f32e): the ask was answered in a chat and the chat only
    # unblocked the card, so nothing here could tell an answered ask from an idle run.
    suspect = krh.relay_suspect(conn, task.id, now=now)
    extra: dict[str, Any] = {"relay_suspect": suspect} if suspect else {}
    if suspect:
        note += (". Suspect: a chat unblocked its needs_input block just before this, with no comment marked "
                 "relays_user_message; if David answered, re-post the answer with kanban_comment "
                 "relays_user_message=true (cross_task: relays_user_message) instead of only unblocking")

    def _flag() -> None:
        if suspect:
            logger.warning("kanban: %s on %s: needs_input unblocked by a chat at %s with no marked relay",
                           krh.SUSPECT_EVENT, task.id, suspect["unblocked_at"])
            kb._append_event(conn, task.id, krh.SUSPECT_EVENT, {"runs": runs, **suspect})

    # Runs inside hold()'s write transaction.
    if task.created_by == CREATED_BY:
        cur = conn.execute("UPDATE tasks SET status = 'triage', claim_lock = NULL, claim_expires = NULL, "
                           "worker_pid = NULL WHERE id = ? AND status IN ('ready', 'review') "
                           "AND current_run_id IS NULL AND " + _UNCLAIMED, (task.id, now))
        if cur.rowcount == 1:
            _flag()
            kb._append_event(conn, task.id, ESCALATED_EVENT, {"runs": runs, "since": marker, "owner": None,
                                                              "status": "triage", "reason": note, **extra})
        return None if cur.rowcount != 1 else ""
    cur = conn.execute("UPDATE tasks SET status = 'todo', claim_lock = NULL, claim_expires = NULL, "
                       "worker_pid = NULL WHERE id = ? AND status IN ('ready', 'review') "
                       "AND current_run_id IS NULL AND " + _UNCLAIMED, (task.id, now))
    if cur.rowcount != 1:
        return None
    _flag()
    key = f"no-progress:{task.id}:{marker}"
    existing = conn.execute("SELECT id, status FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
    owner = existing[0] if existing and existing[1] not in ("done", "archived") else None
    if owner is None:
        supervisor = _supervisor_profile(task)
        body = _owner_body(task, runs, marker)
        if suspect:
            body += ("\n\nRelay suspect (`no_progress_relay_suspect`): a chat unblocked this card's needs_input "
                     "block just before the stop and no comment was marked as carrying David's reply. If he "
                     "answered, post his answer on the card with `kanban_comment(relays_user_message=true)` "
                     "(cross-gateway: `cross_task` with `relays_user_message`); that counts as progress.")
        owner = kb.create_task(
            conn, title=f"Unstick {task.id}: {runs} runs with no progress"[:200],
            body=body, assignee=supervisor, created_by=CREATED_BY,
            workspace_kind="scratch", triage=supervisor is None, idempotency_key=None if existing else key,
            creator_task_id=task.id, session_id=task.session_id, tenant=task.tenant, priority=task.priority,
        )
    kb._link(conn, owner, task.id)
    kb._append_event(conn, task.id, ESCALATED_EVENT, {"runs": runs, "since": marker, "owner": owner,
                                                      "status": "todo", "reason": note, **extra})
    return owner


def hold(conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
         budget: Optional[int] = None) -> Optional[str]:
    """Called by the dispatcher before claiming a ready/review card. None = spawn as usual; otherwise the card
    was moved off the lane and the return names how (``parked`` / ``escalated:<owner>`` / ``triage``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_copy

    lim = limit() if budget is None else int(budget)
    from hermes_cli.kanban_home_transfer import frozen
    if foreign_copy(conn, task_id) or frozen(conn, task_id):
        return None  # a worker-node copy: its home board owns the decision
    now = int(time.time()) if now is None else int(now)
    if decide(conn, task_id, budget=lim, as_of=now + 1) is None:
        return None  # cheap read on every tick; the write lock is taken only for a card over budget
    with kb.write_txn(conn):
        # Decide again under the lock, with a cutoff taken now: progress recorded since the read above must win.
        if frozen(conn, task_id):
            return None
        now = max(now, int(time.time()))
        verdict = decide(conn, task_id, budget=lim, as_of=now + 1)
        if verdict is None:
            return None
        action, runs, marker, park = verdict
        if action == "park":
            done = _repark(conn, task_id, park, runs, marker, now)
        else:
            task = kb.get_task(conn, task_id)
            done = None if task is None else _escalate(conn, task, runs, marker, now)
    if action == "park":
        if done:
            logger.info("kanban: no-progress budget parked %s behind its wake condition (%d runs)", task_id, runs)
            return "parked"
        return None
    owner = done
    if owner is None:
        return None
    logger.info("kanban: no-progress budget escalated %s (%d runs) -> %s", task_id, runs, owner or "triage")
    return f"escalated:{owner}" if owner else "triage"


def held_json(held: list[tuple[str, str]]) -> list[dict]:
    """``dispatch --json`` rows for ``DispatchResult.no_progress_held``."""
    return [{"task_id": tid, "action": action} for (tid, action) in held]


def print_held(held: list[tuple[str, str]]) -> None:
    """``hermes kanban dispatch`` text lines for ``DispatchResult.no_progress_held``."""
    for tid, action in held:
        print(f"No-progress budget ({action}): {tid}")
