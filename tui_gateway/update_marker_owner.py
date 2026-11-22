"""Positive update custody for a refusal; never admission or lease takeover authority."""
import os
from pathlib import Path

from hermes_cli import active_sessions, process_identity, update_lock
from tui_gateway.turn_marker import finite_deadline, marker_writer_state, read_turn_marker


def classify_refusal(home, key, refusal, *, install_root=None):
    """Return a labelled copy only for a live paused writer of this updating install.

    Missing birth, ledger or kernel evidence retains the original actionable refusal.
    A durable lease's PID-only holder string is deliberately insufficient.
    """
    if getattr(refusal, "reason", None) != active_sessions.SESSION_NOT_OWNED:
        return refusal
    if getattr(refusal, "holder_remote", False):
        return refusal
    pid = getattr(refusal, "holder_pid", None)
    if pid is None or (isinstance(pid, int) and not isinstance(pid, bool) and pid == 0):
        return _label(refusal, "lease_unknown", 0, None)
    birth = finite_deadline(getattr(refusal, "holder_start_time", None))
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or birth is None:
        return refusal
    from hermes_cli._startup_fast import project_root_str
    root = Path(install_root) if install_root is not None else Path(project_root_str())
    try:
        rows = process_identity.ledger_entries(project_root=root, verified_only=True)
        same_backend = any(row.get("pid") == pid and row.get("create_time") == birth
                           and row.get("install") == process_identity.install_id(root)
                           and row.get("purpose") in ("serve", "gateway", "dashboard") for row in rows)
        from hermes_cli.host_loop_lease import retiring_reason
        if same_backend and retiring_reason(pid, home=home):
            return _label(refusal, "lease_retiring", pid, birth)
        marker = read_turn_marker(home, key)
        if (not marker or marker.get("reason") != "core_update_pause"
                or marker.get("writer_pid") != pid or marker.get("writer_start_time") != birth
                or marker_writer_state(marker) != "alive"):
            return refusal
        rows = process_identity.ledger_entries(project_root=root, verified_only=True)
        if not any(row.get("pid") == pid and row.get("create_time") == birth
                   and row.get("install") == process_identity.install_id(root) for row in rows):
            return refusal
        from hermes_constants import get_default_hermes_root
        update = update_lock._read_marker(get_default_hermes_root(home=Path(home)) / update_lock.MARKER_NAME)
        if (update is None or update.started_at is None or update.create_time is None
                or update_lock.incarnation_live(update.pid, update.create_time) is not True):
            return refusal
        # Positive lock contention, not checkout_lock_held's conservative "unreadable = held".
        # Windows' lock failure cannot distinguish access failure here; retain its card.
        if os.name != "posix":
            return refusal
        fd = os.open(update_lock.checkout_lock_path(root), os.O_RDONLY)
        try:
            checkout = update_lock._parse_marker(os.read(fd, 4096))
            held = not update_lock._try_shared_lock(fd)
        finally:
            os.close(fd)
        if (not held or checkout.started_at is None or checkout.pid != update.pid
                or checkout.create_time != update.create_time):
            return refusal
        # Revalidate the writer after the update probes (no stale PID classification).
        if marker_writer_state(marker) != "alive" or read_turn_marker(home, key) != marker:
            return refusal
    except (OSError, ValueError, TypeError):
        return refusal
    return _label(refusal, "update_marker", pid, birth)


WAIT_CAUSES = frozenset({"update_marker", "lease_retiring", "lease_unknown"})


def is_transient(refusal):
    return (getattr(refusal, "reason", None) == active_sessions.SESSION_NOT_OWNED
            and getattr(refusal, "ownership_cause", None) in WAIT_CAUSES)


def _label(refusal, cause, pid, birth):
    result = active_sessions.ActiveSessionRefusal(str(refusal), refusal.reason)
    result.ownership_cause = cause
    result.holder_pid = pid
    result.holder_start_time = birth
    return result
