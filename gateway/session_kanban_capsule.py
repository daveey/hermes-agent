"""Native owner capsule requirement: derived, frozen, and fail-closed without live transport. [daveey]

A Kanban card needs a capsule when its claimed task needs ``host_repo`` (workspace kind or declared
requires) while its assignee's actual resolved terminal backend is not ``local``. The owner derives this
itself from the board row and the profile policy, never from caller params, saved policy or config.

``build_kanban_policy`` freezes ``capsule_requirement`` into ``kanban_json`` (audit/recovery shape), but
every admission and launch re-derives it from the board (``require_capsule_transport``): an older saved
policy without the field, or a replayed request with a forged ``False``, cannot weaken it.

Transport: a required card may only proceed once a run-bound, host-owned, authenticated observation
transport is live for exactly ``(db, task_id, run_id, claim_lock)`` of this attempt: the dispatcher host's
rendezvous socket (``gateway.session_kanban_rendezvous``). Admission probes it (``_live_transport_ready``,
never consuming); the launch (``attach_capsule_transport`` inside ``placing_spawn``) authenticates and
consumes it for that one spawn. No rendezvous, or any check failing, refuses with
``capsule_observation_missing``. There is deliberately no boolean, config key, env var or caller param
that can make it ready.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

MISSING = 'capsule_observation_missing'
NOT_REQUIRED = {'required': False}
REQUIRED = {'required': True, 'requires': 'host_repo', 'backend': 'remote'}


def capsule_requirement(task) -> dict:
    """Derived requirement for a claimed task row: REQUIRED or NOT_REQUIRED (unreadable backend → REQUIRED)."""
    from hermes_cli.kanban_capsule_admission import _requires_host_repo
    from hermes_cli import kanban_capabilities
    if not _requires_host_repo(task):
        return dict(NOT_REQUIRED)
    # Same seam native create/claim admission reads for the host_repo grant: a capsule is required exactly
    # when the assignee's own policy does not grant host_repo (non-local backend).
    try:
        granted = 'host_repo' in kanban_capabilities._profile_capabilities(task.assignee, ['host_repo'])
    except Exception:  # health: allow BLE001 -- an unreadable policy cannot prove the grant: require
        granted = False
    return dict(NOT_REQUIRED) if granted else dict(REQUIRED)


def _rederive(context: dict) -> dict:
    from hermes_cli import kanban_db as kb
    db, task_id = context.get('db'), context.get('task_id')
    if not (isinstance(db, str) and db and isinstance(task_id, str) and task_id):
        return dict(REQUIRED)
    try:
        with closing(sqlite3.connect(Path(db).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            task = kb.get_task(conn, task_id)
    except (OSError, ValueError, sqlite3.Error):
        return dict(REQUIRED)
    if task is None:
        return dict(REQUIRED)
    return capsule_requirement(task)


def _live_transport_ready(context: dict) -> bool:
    """Authenticated, run-bound owner transport readiness (admission probe; does not consume the run)."""
    from gateway.session_kanban_rendezvous import probe
    return probe(context)


def _required_context(policy):
    """The frozen Kanban context when the card needs a capsule, else None (non-Kanban / ordinary)."""
    raw = getattr(policy, 'kanban_json', None)
    if raw is None:
        return None
    try:
        context = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError(MISSING) from exc
    if not isinstance(context, dict):
        raise RuntimeStoreError(MISSING)
    frozen = context.get('capsule')
    # The board row re-derives every time; a frozen value can only add a requirement, never remove one.
    required = _rederive(context)['required'] or (frozen is not None and frozen != NOT_REQUIRED)
    return context if required else None


def require_capsule_transport(policy) -> None:
    """Raise ``capsule_observation_missing`` for a capsule-required Kanban policy without live transport.

    Non-Kanban policies and ordinary cards (local backend, or no host_repo need) return unchanged.
    """
    context = _required_context(policy)
    if context is not None and not _live_transport_ready(context):
        raise RuntimeStoreError(MISSING)


def check_capsule_transport(policy) -> None:
    """Launch preflight: ``capsule_observation_missing`` (cause: the precise rendezvous refusal) unless a required
    card's rendezvous is live for this attempt. Never consumes; ordinary cards return unchanged."""
    context = _required_context(policy)
    if context is None:
        return
    from gateway.session_kanban_rendezvous import RendezvousError, check
    try:
        check(context)
    except RendezvousError as exc:
        raise RuntimeStoreError(MISSING) from exc


def attach_capsule_transport(policy):
    """Launch-time: None for ordinary cards; for a required card the authenticated, consumed rendezvous.

    Raises ``capsule_observation_missing`` (cause: the precise rendezvous refusal) when none is established.
    """
    context = _required_context(policy)
    if context is None:
        return None
    from gateway.session_kanban_rendezvous import RendezvousError, attach
    try:
        return attach(context)
    except RendezvousError as exc:
        raise RuntimeStoreError(MISSING) from exc
