"""Shared ready-entry clock registry (not a block-release or reopen policy).

A now-ready card cannot have waited longer than its latest entry/release. Some
entries may first land in todo/review/triage; a later ready entry still wins.
``status`` is intentionally absent: its only lifecycle writer invalidates a
descendant to todo. ``continued`` is conditional and must name status=ready.
"""

READY_ENTRY_EVENT_KINDS = frozenset({
    "created", "promoted", "promoted_manual", "triage_swept",
    "containment_released", "review_reopened", "reopened", "changes_requested",
    "reclaimed", "unblocked", "continuation_woken", "condition_met",
    "wake_probe_stale", "wake_check_stale", "owner_restart_lost",
})

# Execution observations also bound a now-unclaimed interval. Keep these out of
# the entry registry: an unused Board grant is not execution (callers filter it).
READY_WAIT_FLOOR_KINDS = READY_ENTRY_EVENT_KINDS | {"claimed", "spawned"}
