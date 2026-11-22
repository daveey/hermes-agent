"""Audited pending-custody mutation inventory (approved design §8). [daveey]

This inventory stamps nothing, certifies nothing and activates nothing. Historical
and new rows stay UNKNOWN until a separately reviewed capture slice, writer drain
and complete exposure-principal provenance. ORIGINAL_CANDIDATE is not originality.

An exact reviewed test snapshot pins full qualified function names, SQL verbs,
table sets and classifications. It does not discover new writers or prove that
source bodies still match this inventory. No runtime write interception is used.
"""
from __future__ import annotations

ORIGINAL_CANDIDATE = "original_candidate"
RECOVERY_AMBIGUOUS = "recovery_ambiguous"
SYNTHETIC_UNKNOWN = "synthetic_unknown"
IDENTITY_ROTATION = "identity_rotation"
WIRE_FREEZE = "wire_freeze"
STATE_ONLY = "state_only"
REMOVAL = "removal"
GUARD_INSTALL = "guard_install"
NOT_CUSTODY = "not_custody"
DISPOSITIONS = frozenset({
    ORIGINAL_CANDIDATE, RECOVERY_AMBIGUOUS, SYNTHETIC_UNKNOWN, IDENTITY_ROTATION,
    WIRE_FREEZE, STATE_ONLY, REMOVAL, GUARD_INSTALL, NOT_CUSTODY,
})
_O = "kanban_sync_outbox"
_P = "kanban_sync_parent_envelopes"

# (module filename, exact qualified function, SQL verb, resolved table) -> disposition.
SITES: dict[tuple[str, str, str, str], str] = {
    ("kanban_db_sync.py", "_outbox_insert", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_db_sync.py", "_insert_trigger", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_retain_transport.py", "reserve", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_actionability_transport.py", "reserve", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_failure_release.py", "queue", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_db_sync.py", "_queue_rows", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_client.py", "import_board", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_client.py", "repair_missing_children", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_client.py", "retire_lost_placeholder_links", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_compat.py", "enqueue_retry", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_compat.py", "_drop_virtual", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_divergence.py", "repair_divergence", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_omissions.py", "_queue_restore", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_reserved_key.py", "_queue_children", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_reserved_key.py", "_write_receipt", "INSERT", _O): RECOVERY_AMBIGUOUS,
    ("kanban_sync_compat.py", "rejected_parent", "INSERT", _O): SYNTHETIC_UNKNOWN,
    ("kanban_sync_envelope.py", "parent", "INSERT", _P): SYNTHETIC_UNKNOWN,
    ("kanban_containment_outbox.py", "strip_lifecycle", "UPDATE", _O): IDENTITY_ROTATION,
    ("kanban_sync_client.py", "push", "UPDATE", _O): IDENTITY_ROTATION,
    ("kanban_sync_envelope.py", "outbound", "UPDATE", _O): WIRE_FREEZE,
    ("kanban_actionability_transport.py", "eligible_pending", "UPDATE", _O): STATE_ONLY,
    ("kanban_actionability_transport.py", "record_result", "UPDATE", _O): STATE_ONLY,
    ("kanban_retain_transport.py", "record_result", "UPDATE", _O): STATE_ONLY,
    ("kanban_sync_status_origin.py", "bind", "UPDATE", _O): STATE_ONLY,
    ("kanban_actionability_transport.py", "confirm", "DELETE", _O): REMOVAL,
    ("kanban_actionability_transport.py", "record_result", "DELETE", _O): REMOVAL,
    ("kanban_containment_outbox.py", "strip_lifecycle", "DELETE", _O): REMOVAL,
    ("kanban_db_sync.py", "_apply_one", "DELETE", _O): REMOVAL,
    ("kanban_retain_safety.py", "recover", "DELETE", _O): REMOVAL,
    ("kanban_retain_transport.py", "confirm", "DELETE", _O): REMOVAL,
    ("kanban_retain_transport.py", "record_result", "DELETE", _O): REMOVAL,
    ("kanban_sync_client.py", "import_board", "DELETE", _O): REMOVAL,
    ("kanban_sync_client.py", "push", "DELETE", _O): REMOVAL,
    ("kanban_sync_compat.py", "_drop_virtual", "DELETE", _O): REMOVAL,
    ("kanban_sync_dependency_delete.py", "settle", "DELETE", _O): REMOVAL,
    ("kanban_sync_envelope.py", "prune_parents", "DELETE", _P): REMOVAL,
    ("kanban_sync_envelope.py", "retire_parent", "DELETE", _P): REMOVAL,
    ("kanban_sync_rejections.py", "resolve_rejections", "DELETE", _O): REMOVAL,
    ("gen_source_receipt_fixture.py", "_running", "DELETE", _O): REMOVAL,
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _O): GUARD_INSTALL,
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _P): GUARD_INSTALL,
}

# Audited dynamic-table sites. Only the two recovery copies can reach custody;
# the static snapshot declares both resolved custody tables for those entries.
# This is a reviewed declaration, not runtime authorization or write discovery.
_D = "<dynamic-table>"
DYNAMIC_SITES: dict[tuple[str, str, str, str], tuple[str, bool]] = {
    ("kanban_db_recover.py", "_salvage_table", "INSERT", _D): (RECOVERY_AMBIGUOUS, True),
    ("kanban_db_recover.py", "_fill_from_backup", "INSERT", _D): (RECOVERY_AMBIGUOUS, True),
    ("flaky_intake.py", "Journal._initialize", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_candidate_receipts.py", "persist", "DELETE", _D): (NOT_CUSTODY, False),
    ("kanban_containment_connections.py", "bootstrap", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_db.py", "_delete_task_relations", "DELETE", _D): (NOT_CUSTODY, False),
    ("kanban_db_connect.py", "_rebuild_drifted_tables", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "_delete_row", "DELETE", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "_insert_trigger", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "_upsert_row", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "_upsert_row", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "install_or_remove", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_db_sync.py", "repair_ms_timestamps", "UPDATE", _D): (NOT_CUSTODY, False),
    ("kanban_llm_pacing_history.py", "History.record", "DELETE", _D): (NOT_CUSTODY, False),
    ("kanban_scratch_custody_discovery.py", "discover", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_sync_quarantine.py", "adopt", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_sync_quarantine.py", "restore", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_worker_node.py", "_evict_one", "DELETE", _D): (NOT_CUSTODY, False),
}
