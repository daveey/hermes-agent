"""Requeue grants: which cards a Kanban worker may unblock besides its own. [daveey]

Moved out of ``hermes_cli/kanban.py`` (at its line cap); ``kanban`` re-exports every name, so callers are unchanged.

- A self-heal fix card may unblock the single card named in its defect; a card a finished fix card created may do
  the same (``self_heal_requeue_grant``).
- An owner-wake supervisor card (chat-closeout ``supervise_on_board``: ``created_by='owner-wake'``, title
  ``Owner wake: supervise ...``) may unblock exactly the local ``- <board>/<id>`` card lines of its body,
  only while each is in triage or blocked (``requeue_grant``); the tool resolves cross-board grants.
"""
from __future__ import annotations

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


def requeue_grant(conn, worker_task_id: str, target_id: str, *,
                  board: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """``(via, source_card)`` when this worker may unblock ``target_id``, else ``(None, None)``.

    - ``self-heal-requeue``: the self-heal grant (``self_heal_requeue_grant``), unchanged.
    - ``owner-wake-requeue``: an owner-wake supervisor card (``created_by='owner-wake'``, title
      ``Owner wake: supervise ...``) may unblock exactly the cards its body lists on this board,
      and only while the target is in triage or blocked.

    Every other worker and target stays refused. ``board`` is the board ``conn`` is on (default:
    the active board). [daveey]
    """
    target, fix_id = self_heal_requeue_grant(conn, worker_task_id)
    if target is not None and target == target_id:
        return "self-heal-requeue", fix_id
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
    return bool(owner_wake_listed_refs(worker))
