"""Admission-only destination evidence from the authenticated enrollment registry.

GET /v2/machines reads active Mailbox enrollments directly (no pagination).
Never use the synced gateway discovery records or the worker-node cache here.
Enrollment is not origin authority, placement permission, or receiver liveness.
"""
import re


_COMPONENT = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}"


def require_destination(inventory, *, gateway, destination):
    """Validate a fresh complete registry reply before accepting its membership."""
    if (not isinstance(inventory, dict) or inventory.get("me") != gateway
            or not isinstance(inventory.get("machines"), list)):
        raise ValueError("origin_forward_inventory_unavailable")
    names = set()
    for row in inventory["machines"]:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or re.fullmatch(_COMPONENT, row["name"]) is None
                or row.get("state") != "active" or row["name"] in names):
            raise ValueError("origin_forward_inventory_unavailable")
        names.add(row["name"])
    # The authenticated caller must appear in the same active registry snapshot.
    if gateway not in names:
        raise ValueError("origin_forward_inventory_unavailable")
    if destination not in names:
        raise ValueError("origin_forward_destination_unknown")
