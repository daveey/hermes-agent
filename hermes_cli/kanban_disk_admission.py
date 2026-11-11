"""Opt-in installation-root disk brake for new Kanban claims only. [daveey]

Workers set kanban.admission.disk_free_floor_bytes (10 GiB). Unset hosts retain
existing behavior. Sample the actual root filesystem at every prelease/final
claim boundary: no stale timer receipt, remote admin token, or shared hold to
release. This stops new admissions, not writes from already-running producers.
"""
import os
from pathlib import Path


def refusal_reason():
    """Return None when disabled/healthy, otherwise a typed defer reason.

    Read the installation root rather than a task/profile config so every
    dispatcher on a worker observes the same floor. Unknown configured state
    fails closed. Recovery is automatic at the floor; no operator state changes.
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / 'config.yaml')
    except FileNotFoundError:
        return None
    except Exception:  # health: allow BLE001 -- unreadable admission policy must refuse claims
        return 'worker_disk_policy_unknown'
    if doc is None:
        return None
    if not isinstance(doc, dict):
        return 'worker_disk_policy_unknown'
    kanban = doc.get('kanban', {})
    if kanban is None:
        return None
    if not isinstance(kanban, dict):
        return 'worker_disk_policy_unknown'
    admission = kanban.get('admission', {})
    if admission is None:
        return None
    if not isinstance(admission, dict):
        return 'worker_disk_policy_unknown'
    if 'disk_free_floor_bytes' not in admission:
        return None
    floor = admission['disk_free_floor_bytes']
    if type(floor) is not int or floor <= 0:
        return 'worker_disk_policy_unknown'
    try:
        stat = os.statvfs('/')
        available = stat.f_bavail * stat.f_frsize
    except OSError:
        return 'worker_disk_unknown'
    if available < 0:
        return 'worker_disk_unknown'
    return 'worker_disk_below_floor' if available < floor else None
