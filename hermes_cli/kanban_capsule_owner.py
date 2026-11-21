"""Submitter-side owner binding for capsule-required Kanban cards. [daveey]

The capsule gates (``gateway.session_kanban_capsule``: admission probe, launch-time rendezvous consume,
``--observation-fd`` on the managed worker) live in the PROFILE OWNER, not in the dispatcher or the
submitter. ``kanban_worker_client`` reaches whatever owner ordinary discovery returns for its home, and a
named profile is served by the root multiplexer unless it is ``gateway.standalone``. When that owner runs
a core without these gates (an older or unrelated install), it admits the card and runs an ordinary
managed worker: real model and tool calls, no capsule, no observation (2026-11-02, hub, run4 of
t_01a19a6b4fcce737: pinned-core submitter, global-core multiplexer owner).

So the submitter itself refuses BEFORE ``kanban.run`` (no session, no admission, no model or tool work)
unless the discovered owner proves both:

* it advertises ``CAPABILITY`` in its control identity (only a core carrying the capsule gates publishes
  it), and
* its ``code_sha`` equals this submitter's own code identity, OR it names a strict
  full revision and advertises the reviewed ``CONTRACT_CAPABILITY``. The latter
  contract requires the owner and its managed child to enforce clean boot/disk
  identity and installed provenance at each capsule startup boundary. Legacy owners
  remain exact-revision only; an unknown submitter identity is always refused.

Ordinary cards (no ``host_repo`` need, or an assignee whose own policy grants ``host_repo``) are not
affected. The requirement is derived from the board row exactly as the owner derives it; an unreadable
board or task fails closed (required).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_cli.gateway_client import GatewayUnavailableError

CAPABILITY = 'kanban-capsule-transport-v1'
# Owners advertise this only with launch-time code-identity enforcement. A legacy
# owner remains usable only by its own exact-revision submitter.
CONTRACT_CAPABILITY = 'kanban-capsule-contract-v2'
REFUSED = 'kanban_capsule_owner_unsupported'


class CapsuleOwnerRefused(GatewayUnavailableError):
    """A final refusal (never a restart wait): the discovered owner cannot host this capsule card."""

    def __init__(self, detail: str):
        super().__init__(f'{REFUSED}: {detail}', 'incompatible', REFUSED)


def card_requires_capsule(board_db, task_id) -> bool:
    """The owner's own derivation (``capsule_requirement``) on a read-only board row; unreadable -> True."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_lockguard
    from gateway.session_kanban_capsule import capsule_requirement
    try:
        uri = Path(board_db).resolve(strict=True).as_uri() + '?mode=ro'
        with closing(kanban_lockguard.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            task = kb.get_task(conn, task_id)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return True
    if task is None:
        return True
    return bool(capsule_requirement(task).get('required', True))


def own_code_sha() -> str | None:
    from hermes_cli.version_info import get_code_identity
    sha = get_code_identity().get('sha')
    return sha if isinstance(sha, str) and sha else None


def owner_check(required: bool):
    """``connect_gateway(owner_check=...)`` callback: None for ordinary cards, else the binding check."""
    if not required:
        return None
    expected = own_code_sha()

    def check(endpoint):
        capabilities = getattr(endpoint, 'capabilities', None) or frozenset()
        if CAPABILITY not in capabilities:
            raise CapsuleOwnerRefused('owner lacks ' + CAPABILITY)
        if expected is None:
            raise CapsuleOwnerRefused('submitter code identity unknown')
        actual = getattr(endpoint, 'code_sha', None)
        if (isinstance(actual, str) and len(actual) == 40
                and all(ch in '0123456789abcdef' for ch in actual)
                and CONTRACT_CAPABILITY in capabilities):
            return
        if actual != expected:
            raise CapsuleOwnerRefused(f'owner code {str(actual)[:12] or "unknown"} != submitter {expected[:12]}')
    return check
