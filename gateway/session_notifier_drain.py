"""Selective queued notifier admission during a core-update claim hold.

Receipts stay queued; only their execution waits. Human inputs and other producers
retain their normal admission semantics. Selection is repeated inside the store's
claim transaction so async route validation cannot authorize a different input.
"""
import json
from pathlib import Path

# [daveey] Stamped (mtime = last seen) whenever a notifier leg finds the hold on, in the fleet root so every profile
# and process reads the same one: the hold marker is gone once it lifts, but the ``tui`` consumers measure their
# defer window from the lift (gateway.kanban_wake_sibling), not from the event.
SEEN_RELPATH = Path("core-update") / "notifier-hold-seen"


def _seen_path():
    from agent.file_safety import _hermes_root_path
    return _hermes_root_path() / SEEN_RELPATH


def notifier_wake(row):
    payload = row['payload']
    descriptor = payload.get('local_automation_v1') or (payload.get('native_text_v1') or {}).get('automation') or {}
    identity = descriptor.get('identity')
    if identity != row['request_id'] or not row['principal_id'].startswith('automation:'):
        return False
    try:
        parts = json.loads(identity)
    except (TypeError, ValueError):
        return False
    return (isinstance(parts, list) and len(parts) == 3 and parts[0] == 'kanban-wake'
            and row['principal_id'] == 'automation:' + str(parts[1]))


def held():
    from agent.core_update_drain import active_hold, hold_paths
    from hermes_cli.admission_fence import is_held
    return active_hold() is not None or any(is_held(path.parent) for path in hold_paths())


def held_noted():
    """:func:`held`, stamping :data:`SEEN_RELPATH` while it is on. A failed stamp only loses the lift time."""
    on = held()
    if on:
        try:
            path = _seen_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError:
            pass
    return on


def last_held_at():
    """When a notifier leg last saw the hold on (Unix seconds), or None (never, or unreadable)."""
    try:
        return _seen_path().stat().st_mtime
    except OSError:
        return None


def quiet_active():
    """A verified claim-quiet lease (``agent.claim_quiet``, read-only; every uncertain outcome is open)."""
    from agent.claim_quiet import read_active
    return read_active() is not None


def eligible(row):
    # Only a classified Kanban wake EXECUTION waits, for the updater hold or a verified claim-quiet lease;
    # ``held`` itself stays updater-only. The receipt stays queued under its identity; humans never wait.
    return not notifier_wake(row) or (not held() and not quiet_active())


def queued_head(pending):
    return next((row for row in pending if row['status'] == 'queued' and eligible(row)), None)
