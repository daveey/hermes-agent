"""Shared ready-entry clock registry (not a block-release or reopen policy).

A now-ready card cannot have waited longer than its latest entry/release. Some
entries may first land in todo/review/triage; a later ready entry still wins.
``status`` is intentionally absent: its only lifecycle writer invalidates a
descendant to todo. Conditional kinds (``continued``, ``shared_blocker_cleared``)
must name status=ready. A ``shared_blocker_cleared`` without status retires a
hold on a card that stayed ready/review or stayed scheduled on its own wait;
that is not a ready entry. The blocked-card branch is already covered by
``unblocked``.
"""

READY_ENTRY_EVENT_KINDS = frozenset({
    "created", "promoted", "promoted_manual", "triage_swept",
    "containment_released", "review_reopened", "reopened", "changes_requested",
    "reclaimed", "unblocked", "continuation_woken", "condition_met",
    "wake_probe_stale", "wake_check_stale", "owner_restart_lost",
    "wake_check_broken", "wake_check_lost", "ci_run_unstartable",
    "wake_check_dead_ancestry", "external_ref_broken", "external_service_down",
    "supervisor_request_wake",
})

# Count only when payload names status=ready. ``continued`` is a checkpoint;
# ``shared_blocker_cleared`` is also appended without a status change.
READY_ENTRY_CONDITIONAL_KINDS = frozenset({"continued", "shared_blocker_cleared"})

# Execution observations also bound a now-unclaimed interval. Keep these out of
# the entry registry: an unused Board grant is not execution (callers filter it).
READY_WAIT_FLOOR_KINDS = READY_ENTRY_EVENT_KINDS | {"claimed", "spawned"}


def conditional_ready_entry_sql(kind_expr: str = "kind", payload_expr: str = "payload") -> str:
    """SQL predicate: a conditional kind whose payload names status=ready."""
    kinds = ", ".join(f"'{k}'" for k in sorted(READY_ENTRY_CONDITIONAL_KINDS))
    return f"({kind_expr} IN ({kinds}) AND json_extract({payload_expr}, '$.status') = 'ready')"
