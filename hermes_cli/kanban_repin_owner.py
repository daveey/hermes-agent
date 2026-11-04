"""Bounded recovery of an orphaned worker-placement wait. No placement changes. [daveey]

A worker-first card no pool worker can run waits for its owner to repin it (``kanban_repin_notice``). An owner is
a live recipient chat (link or subscription) or an open creator card. When neither remains, the origin creates ONE
owner-wake supervisor card on the same board (idempotency key ``repin-owner:<id>``); its worker may repin exactly
that card to its origin through ``kanban_move`` (``kanban_creator_repin.supervisor_refusal``). Nothing here repins.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SUPERVISOR_TITLE = "Owner wake: supervise"
CLOSED = ("done", "archived")


def idempotency_key(task_id):
    return f"repin-owner:{task_id}"


def _session_state(path, session):
    """True/False for a session found in ``path``; ``"missing"`` when absent; None when unreadable."""
    from hermes_cli import kanban_lockguard
    from hermes_cli.kanban_origin_adapter import _resolve_session

    try:
        db = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            tip = _resolve_session(path, session, db)
            row = db.execute("SELECT archived, ended_at, end_reason FROM sessions WHERE id=?", (tip,)).fetchone()
            return bool(row and not any(row))
        finally:
            db.close()
    except ValueError as exc:
        return "missing" if str(exc) == "origin_session_missing" else None
    except (sqlite3.Error, OSError):
        return None


def _local_stores():
    """Every local profile's ``state.db`` (default root, named profiles, the active home), or None if unlistable."""
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import _get_default_hermes_home, _iter_named_profile_dirs

    try:
        homes = [_get_default_hermes_home(), *_iter_named_profile_dirs(), get_hermes_home()]
    except (OSError, ValueError):
        return None
    seen, stores = set(), []
    for home in homes:
        path = Path(home) / "state.db"
        key = str(path.absolute())
        if key not in seen:
            seen.add(key)
            stores.append(path)
    return stores


def _any_profile_alive(session):
    """A recipient with no recorded profile: its state in the local store(s) that hold it.

    Live in any store: alive. Not found anywhere, an unreadable store, or no store at all: unknown (None),
    never death. Dead only when every store that holds it says so and every store was readable.
    """
    stores = _local_stores()
    if stores is None:
        return None
    found, unknown = set(), False
    for path in stores:
        if not path.exists():
            continue
        state = _session_state(path, session)
        if state is None:
            unknown = True
        elif state != "missing":
            found.add(state)
    if True in found:
        return True
    if unknown or not found:
        return None
    return False


def chat_alive(gateway, profile, session, *, local_gateway):
    """True/False/None: unknown remote, unknown profile or unreadable state is never proof of death."""
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir

    if gateway != local_gateway:
        return None
    if not profile:
        return _any_profile_alive(session)
    try:
        home = get_hermes_home() if profile == get_active_profile_name() else get_profile_dir(profile)
    except (OSError, ValueError):
        return None
    path = home / "state.db"
    if not path.exists():
        return None  # an unavailable profile store proves nothing about its chats
    state = _session_state(path, session)
    return False if state == "missing" else state


def recipient_states(conn, task_id, gateway):
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_origin import parse_origin, OriginError

    recipients = {(r['chat_gateway'], r['chat_profile'], r['chat_session'])
                  for r in _chat_links(conn, task_id)}
    for sub in conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=?", (task_id,)):
        if sub['platform'] == 'tui':
            recipients.add((gateway, sub['notifier_profile'], sub['chat_id']))
        elif sub['platform'] == 'hub':
            try:
                origin = parse_origin(sub['chat_id'])
                recipients.add((origin.gateway, origin.profile, origin.session))
            except OriginError:
                yield None
        else:
            yield None  # external messaging subscriber: cannot establish death from local state
    for gw, profile, session in recipients:
        yield chat_alive(gw, profile, session, local_gateway=gateway)


def has_owner(conn, task_id, gateway):
    """A live (or not provably dead) recipient, or an open creator card.

    An open creator always owns the wait: ``tasks.session_id`` is the chat the creator card came from, not its
    worker, so that chat ending says nothing about whether the creator card will still act.
    """
    from hermes_cli.kanban_archive_scope import _creator_task_id

    if any(state is not False for state in recipient_states(conn, task_id, gateway)):
        return True
    creator = _creator_task_id(conn, task_id)
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (creator,)).fetchone() if creator else None
    return row is not None and row['status'] not in CLOSED


def supervisor(conn, task_id):
    """``(id, status)`` of this card's supervisor (open or closed), or None."""
    row = conn.execute("SELECT id, status FROM tasks WHERE idempotency_key=? ORDER BY created_at LIMIT 1",
                       (idempotency_key(task_id),)).fetchone()
    return (row[0], row[1]) if row else None


def supervisor_id(conn, task_id):
    found = supervisor(conn, task_id)
    return found[0] if found else None


def wait_reason(conn, task_id):
    """``waiting_for_manual_repin`` when owned, else ``...:no_owner:<supervisor id>:<its status>`` (or
    ``...:no_owner:pending`` before one exists). A closed supervisor stays named with its status, never as live."""
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_repin_notice import MANUAL_REPIN

    gateway = (sync_state(conn) or {}).get('gateway_id')
    try:
        if has_owner(conn, task_id, gateway):
            return MANUAL_REPIN
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("repin owner check for %s failed (%s)", task_id, type(exc).__name__)
        return MANUAL_REPIN
    found = supervisor(conn, task_id)
    return f"{MANUAL_REPIN}:no_owner:" + (f"{found[0]}:{found[1]}" if found else "pending")


def board_slug(conn):
    """The slug of the board ``conn`` actually opened (not the Board sync id)."""
    from hermes_cli.kanban_db_sync import board_slug_for_path

    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return board_slug_for_path(row[2] or "")
    return "default"


def title(board, task_id):
    return f"{SUPERVISOR_TITLE} {board}/{task_id} (no live repin owner)"


def body(board, task_id, gateway):
    origin = f"machine:{gateway}"
    return (f"[owner-wake] No live recipient or open creator owns this worker-placement wait:\n"
            f"- {board}/{task_id}: waiting for a manual repin\n\n"
            "Read the card and its required labels. Within its approved plan, arrange a capable worker, or repin "
            "it to its origin with the kanban_move TOOL (not the shell: `hermes kanban move` is fenced in worker "
            f"shells): kanban_move(task_id=\"{task_id}\", runs_on=[\"{origin}\"], reason=\"no pool worker can run "
            f"it\"). The only allowed target is exactly runs_on=[\"{origin}\"]. This supervisor does not "
            "automatically repin the original. Do not re-park on the same premise. Check reverse dependencies "
            "before closing any card; report what changed. Ask the user only for a genuine human decision.")


def ensure_supervisor(conn, task_id, gateway, fenced):
    """One supervisor per card, ever (a closed one is reported, not replaced). ``fenced(row)``: the origin fence.

    Runs after the notice committed, in its own transaction; a failure here leaves the notice intact and the next
    sweep retries. Returns the supervisor id or None.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import load_config_readonly

    if has_owner(conn, task_id, gateway):
        return None
    existing = supervisor_id(conn, task_id)
    if existing:
        return existing
    board = board_slug(conn)
    assignee = (load_config_readonly().get("kanban") or {}).get("orchestrator_profile") or "default"
    # No post-commit side effects: under a caller's transaction this is a savepoint that rolls back alone.
    with kb.write_txn(conn, allow_nested=True):
        existing = supervisor_id(conn, task_id)
        if existing:
            return existing
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not fenced(row):
            return None
        return kb.create_task(
            conn, title=title(board, task_id),
            body=body(board, task_id, gateway),
            assignee=assignee, created_by="owner-wake", board=board, workspace_kind="scratch",
            runs_on=[f"machine:{gateway}"], placement_policy=None,
            idempotency_key=idempotency_key(task_id))
