"""Requeue grants: which cards a Kanban worker may unblock besides its own. [daveey]

Moved out of ``hermes_cli/kanban.py`` (at its line cap); ``kanban`` re-exports every name, so callers are unchanged.

- A self-heal fix card may unblock the single card named in its defect; a card a finished fix card created may do
  the same (``self_heal_requeue_grant``).
- An owner-wake supervisor card (chat-closeout ``supervise_on_board``: ``created_by='owner-wake'``, title
  ``Owner wake: supervise ...``) may unblock exactly the local ``- <board>/<id>`` card lines of its body,
  only while each is in triage or blocked (``requeue_grant``); the tool resolves cross-board grants.
- A missing-parent repair card may unblock exactly the child whose newest ``parent_missing`` block names it
  (``missing_parent_repair_grant``); the operator call's home check uses the same rule across gateways.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from hermes_cli import kanban_db as kb


def _creator_task_id(conn, task_id: str) -> Optional[str]:
    from hermes_cli.kanban import _creator_task_id as creator
    return creator(conn, task_id)


_SELF_HEAL_FIX_KEY = re.compile(r"^selfheal:[a-z0-9]+(?:-[a-z0-9]+)*(?::r[1-9]\d*)?:fix$")
_NAMED_DEFECT_CARD = re.compile(
    r"\bkanban card (t_[0-9a-f]{16}|t_[0-9a-f]{8})\b", re.IGNORECASE,
)
_PLURAL_DEFECT = re.compile(r"\bkanban cards\b", re.IGNORECASE)


def _is_self_heal_fix_card(task) -> bool:
    """A self-heal FIX card, not an investigation (``created_by`` alone is both)."""
    if task is None or (task.created_by or "") != "self-heal":
        return False
    return bool(_SELF_HEAL_FIX_KEY.fullmatch(task.idempotency_key or ""))


def named_defect_card(task) -> Optional[str]:
    """The one ``kanban card t_...`` id in title/body, else None.

    A sibling batch (``kanban cards``) or two different ids is not a single
    defect, so it is not a grant.
    """
    if task is None:
        return None
    text = f"{task.title or ''}\n{task.body or ''}"
    if _PLURAL_DEFECT.search(text):
        return None
    found = list(dict.fromkeys(_NAMED_DEFECT_CARD.findall(text)))
    return found[0] if len(found) == 1 else None


def self_heal_requeue_grant(conn, worker_task_id: str) -> tuple[Optional[str], Optional[str]]:
    """``(target_id, fix_card_id)`` this worker may unblock, else ``(None, None)``.

    A self-heal fix card may unblock the single card named in its defect. A
    card that fix card created may do the same once the fix card is done, so a
    finished repair is not stranded when the fix card can no longer call tools.
    Any other worker, and any other target, stays refused. [daveey]
    """
    worker = kb.get_task(conn, worker_task_id)
    if worker is None:
        return None, None
    if _is_self_heal_fix_card(worker):
        target = named_defect_card(worker)
        return (target, worker.id) if target else (None, None)
    creator = _creator_task_id(conn, worker_task_id)
    if not creator:
        return None, None
    fix = kb.get_task(conn, creator)
    if fix is None or not _is_self_heal_fix_card(fix) or fix.status != "done":
        return None, None
    target = named_defect_card(fix)
    return (target, fix.id) if target else (None, None)


MISSING_PARENT_DETECTOR = "missing-parent-detector"
MISSING_PARENT_VIA = "missing-parent-repair"


def is_missing_parent_repair_card(task) -> bool:
    """A repair card the dispatcher's missing-parent detector filed (``kanban_db_graph.missing_parent_repair``)."""
    return task is not None and (task.created_by or "") == MISSING_PARENT_DETECTOR


def missing_parent_repair_grant(conn, worker_task_id: Optional[str], target_id: str) -> Optional[str]:
    """The repair card id when ``worker_task_id`` may unblock ``target_id``, else None.

    The dispatcher's parent_missing block carries no agent actor, yet it names a repair card as the owner of
    recovery. That repair card (``created_by='missing-parent-detector'``) may unblock exactly the child that
    created it (``creator_task_id``), and only while the child is blocked, has no human request, and its NEWEST
    ``blocked`` event is ``source: parent_missing`` naming this very card as ``repair_task_id``. A later hand
    block, a block naming another repair card, or any release event after it (``current_block_payload``)
    revokes it. The failure breaker's real trip (``kanban_failure_accounting``) lands the child in ``triage``,
    which the status check already refuses; the ``gave_up`` arm is defence in depth for a re-hold that writes
    no ``blocked`` event. Both entry paths use it: the ``kanban_unblock`` tool (same gateway) and the operator
    call's home check (another gateway). [daveey]
    """
    if not worker_task_id or not target_id or worker_task_id == target_id:
        return None
    if not is_missing_parent_repair_card(kb.get_task(conn, worker_task_id)):
        return None
    if _creator_task_id(conn, worker_task_id) != target_id:
        return None
    row = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (target_id,)).fetchone()
    if row is None or row[0] != "blocked" or row[1]:
        return None
    payload = current_block_payload(conn, target_id)
    if payload.get("source") != "parent_missing":
        return None
    return worker_task_id if payload.get("repair_task_id") == worker_task_id else None


# INVARIANT (this grant and ``kanban_operator_call._agent_block`` rely on it): every write that puts a card into
# ``blocked`` appends a ``blocked`` event (``kanban_db.block_task``, the missing-parent detector,
# ``kanban_continuation._enforce_wake_invariant``, ``kanban_source_refusal_block``). While it holds, the newest
# ``blocked`` event of a blocked card describes its current hold. This tuple is NOT every way a card leaves
# ``blocked`` (``reclaimed``, ``kanban_awaiting`` blocked->todo, ``kanban_timed_park`` blocked->scheduled and the
# ``block_policy`` escalation blocked->triage are other exits, safe while the invariant holds, since any later hold
# appends a fresh ``blocked`` event). It is the fallback set for a re-hold path that skips the ``blocked`` event: a
# ``blocked`` event older than any of these is over, so ``current_block_payload`` returns ``{}`` and every grant
# refuses. A new path that re-holds a card WITHOUT a ``blocked`` event must append one; if it cannot, the event kind
# the card left ``blocked`` with before that re-hold must be listed here, or the stale payload (its actor, its
# ``repair_task_id``) grants a release of the new hold. Listed kinds: ``unblocked`` (every unblock path),
# ``promoted`` (the readiness pass leaves ``blocked`` with no ``unblocked``; ``recovery_held`` keeps it off a card
# whose newest ``blocked`` event is unreleased today, so this arm only keeps the helper true to its contract),
# ``promoted_manual`` (``hermes kanban promote`` from blocked) and ``gave_up``. ``gave_up``
# is defence in depth: the breaker's real trip (``kanban_failure_accounting``) lands in ``triage``, which both
# callers already refuse on status. [daveey]
BLOCK_RELEASE_EVENTS = ("unblocked", "promoted", "promoted_manual", "gave_up")


def current_block_payload(conn, task_id: str) -> dict:
    """The payload of the ``blocked`` event that placed the card's current hold, else ``{}``.

    That is the newest ``blocked`` event, unless a ``BLOCK_RELEASE_EVENTS`` event is newer: then the card was
    released after it, and any hold it is in now came from a write with no ``blocked`` event (see the invariant on
    ``BLOCK_RELEASE_EVENTS``). ``{}`` names no source and no actor, so every grant keyed on them refuses. [daveey]
    """
    ev = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                      "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    marks = ", ".join("?" * len(BLOCK_RELEASE_EVENTS))
    if ev is None or conn.execute(
            f"SELECT 1 FROM task_events WHERE task_id = ? AND kind IN ({marks}) AND id > ? LIMIT 1",
            (task_id, *BLOCK_RELEASE_EVENTS, ev[0])).fetchone() is not None:
        return {}
    try:
        payload = json.loads(ev[1] or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


_OWNER_WAKE_TITLE = "Owner wake: supervise"
# One chat-closeout ``owner_wake.card_line`` of a local card: ``- <board>/<id> "<title>": ...`` or ``- <board>/<id>:``.
# A remote card's line names its gateway (`` on <gw>``) or a longer key, so it never matches.
_OWNER_WAKE_CARD_LINE = re.compile(r"^- ([a-z0-9][a-z0-9_-]*)/(t_[0-9a-f]+)(?: \"|:)")
OWNER_WAKE_REQUEUE_STATUSES = ("triage", "blocked")


def _is_owner_wake_supervisor(task) -> bool:
    """A board-supervisor card the chat-closeout owner wake created for an orphaned owner's cards."""
    return (task is not None and (task.created_by or "") == "owner-wake"
            and (task.title or "").startswith(_OWNER_WAKE_TITLE))


def owner_wake_listed_refs(task) -> list[tuple[str, str]]:
    """Exact local (board, id) grants in the initial card-line block only."""
    if not _is_owner_wake_supervisor(task):
        return []
    head = (task.body or "").split("\n\n", 1)[0]
    return list(dict.fromkeys(
        (m.group(1), m.group(2)) for line in head.splitlines()
        if (m := _OWNER_WAKE_CARD_LINE.match(line))))


def owner_wake_listed_cards(task, board: str) -> list[str]:
    """Card ids listed for exactly this board; later prose is never a grant."""
    return [tid for slug, tid in owner_wake_listed_refs(task) if slug == board]


def owner_wake_authority_reopen_grant(conn, worker_task_id: str, authority_id: str, *,
                                      board: Optional[str] = None, expected_run_id: Optional[int] = None,
                                      worker_conn=None, target_id: Optional[str] = None,
                                      ) -> Optional[str]:
    """Return the supervisor id when it may reopen one nominated done authority.

    The authority is not itself listed in the owner-wake body: the body lists the
    contained cards. Resolve those cards' authenticated ``contained`` events and
    allow only the authority named by one of them. This is intentionally separate
    from ``requeue_grant`` so ordinary unblock and ``resume_contained`` cannot gain
    the reopen privilege.
    """
    if not worker_task_id or not authority_id:
        return None
    worker = kb.get_task(worker_conn if worker_conn is not None else conn, worker_task_id)
    if worker is None or not _is_owner_wake_supervisor(worker) or worker.status != "running":
        return None
    if expected_run_id is None or worker.current_run_id != expected_run_id:
        return None
    runs = worker_conn if worker_conn is not None else conn
    live = runs.execute("SELECT status FROM task_runs WHERE id=? AND task_id=?",
                        (expected_run_id, worker_task_id)).fetchone()
    if live is None or live[0] != "running":
        return None
    slug = board or kb.get_current_board()
    listed = owner_wake_listed_cards(worker, slug)
    if not listed:
        return None
    authority = kb.get_task(conn, authority_id)
    if authority is None or authority.status != "done":
        return None
    for listed_id in listed:
        if target_id is not None and listed_id != target_id:
            continue
        target = kb.get_task(conn, listed_id)
        episode = getattr(target, "containment_episode", None) if target is not None else None
        if not episode:
            continue
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND uid=? AND kind='contained'",
            (listed_id, episode),
        ).fetchone()
        if row is None:
            continue
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        policy = payload.get("policy") if isinstance(payload, dict) else None
        named = policy.get("authority") if isinstance(policy, dict) else None
        if (isinstance(named, dict) and named.get("task_id") == authority_id
                and named.get("board") == slug):
            return worker_task_id
    return None


def requeue_grant(conn, worker_task_id: str, target_id: str, *,
                  board: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """``(via, source_card)`` when this worker may unblock ``target_id``, else ``(None, None)``.

    - ``self-heal-requeue``: the self-heal grant (``self_heal_requeue_grant``), unchanged.
    - ``missing-parent-repair``: a missing-parent repair card on its parent_missing child
      (``missing_parent_repair_grant``).
    - ``owner-wake-requeue``: an owner-wake supervisor card (``created_by='owner-wake'``, title
      ``Owner wake: supervise ...``) may unblock exactly the cards its body lists on this board,
      and only while the target is in triage or blocked.

    Every other worker and target stays refused. ``board`` is the board ``conn`` is on (default:
    the active board). [daveey]
    """
    target, fix_id = self_heal_requeue_grant(conn, worker_task_id)
    if target is not None and target == target_id:
        return "self-heal-requeue", fix_id
    repair = missing_parent_repair_grant(conn, worker_task_id, target_id)
    if repair is not None:
        return MISSING_PARENT_VIA, repair
    worker = kb.get_task(conn, worker_task_id)
    if not _is_owner_wake_supervisor(worker):
        return None, None
    slug = board or kb.get_current_board()
    if target_id not in owner_wake_listed_cards(worker, slug):
        return None, None
    task = kb.get_task(conn, target_id)
    if task is None or task.status not in OWNER_WAKE_REQUEUE_STATUSES:
        return None, None
    return "owner-wake-requeue", worker.id


def has_requeue_grant(conn, worker_task_id: str, *, board: Optional[str] = None) -> bool:
    """Whether this worker holds any requeue grant (it then gets the ``kanban_unblock`` tool). [daveey]"""
    if self_heal_requeue_grant(conn, worker_task_id)[0] is not None:
        return True
    worker = kb.get_task(conn, worker_task_id)
    # A missing-parent repair card gets the tool even when its child lives on another gateway: the tool then
    # forwards the unblock as an operator call, and the child's home re-checks the grant.
    return bool(owner_wake_listed_refs(worker)) or is_missing_parent_repair_card(worker)
