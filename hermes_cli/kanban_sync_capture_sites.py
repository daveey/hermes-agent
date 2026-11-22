"""Audited inventory of every pending-custody mutation site. [daveey]

Post-collection replay fencing (POST-COLLECTION-DESIGN section 8) requires a COMPLETE
insert/update/delete inventory of ``kanban_sync_outbox`` and
``kanban_sync_parent_envelopes`` before any capture may be certified ``original``.
This module is that inventory and nothing else: it stamps nothing, certifies nothing
and activates nothing. Historical and new rows stay UNKNOWN until a later, separately
reviewed slice stamps ORIGINAL_CANDIDATE sites after writer drain and complete
exposure-principal provenance. A producer's name is never proof of originality, so even
ORIGINAL_CANDIDATE only means "may be certified once the unresolved-predecessor and
pre-I/O exposure conditions hold at that site".

``tests/hermes_cli/test_kanban_sync_capture_sites.py`` scans the source and fails when a
site appears, disappears or changes verb without an entry here.
"""
from __future__ import annotations

# New, authorized intent with no recovery semantics: the only sites that may ever be stamped original.
ORIGINAL_CANDIDATE = "original_candidate"
# Re-emission / recovery / repair of possibly exposed work: stamp ambiguous, never original.
RECOVERY_AMBIGUOUS = "recovery_ambiguous"
# Re-derived synthetic parent or synthetic verdict: UNKNOWN provenance, never original.
SYNTHETIC_UNKNOWN = "synthetic_unknown"
# In-place op_id / created_at / payload rotation of an existing record: must taint sticky-ambiguous.
IDENTITY_ROTATION = "identity_rotation"
# Freezes the exact wire bytes/HLC of an existing record (an exposure indicator, not a certification).
WIRE_FREEZE = "wire_freeze"
# Attempt/error/state/status-origin bookkeeping; changes no identity tuple.
STATE_ONLY = "state_only"
# Removes pending custody (collection of provenance with its record).
REMOVAL = "removal"
# Installs the immutability / taint guards themselves.
GUARD_INSTALL = "guard_install"

# Dynamic-table statement audited as unable to name either custody table (fixed table tuple / other store).
NOT_CUSTODY = "not_custody"

DISPOSITIONS = frozenset({
    ORIGINAL_CANDIDATE, RECOVERY_AMBIGUOUS, SYNTHETIC_UNKNOWN, IDENTITY_ROTATION,
    WIRE_FREEZE, STATE_ONLY, REMOVAL, GUARD_INSTALL, NOT_CUSTODY,
})

_O = "kanban_sync_outbox"
_P = "kanban_sync_parent_envelopes"

# (module file, enclosing top-level function, SQL verb, table) -> disposition
SITES: dict[tuple[str, str, str, str], str] = {
    # -- new intents ------------------------------------------------------------------------
    ("kanban_db_sync.py", "_outbox_insert", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_db_sync.py", "_insert_trigger", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_retain_transport.py", "reserve", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_actionability_transport.py", "reserve", "INSERT", _O): ORIGINAL_CANDIDATE,
    ("kanban_failure_release.py", "queue", "INSERT", _O): ORIGINAL_CANDIDATE,
    # -- recovery / re-emission / repair ------------------------------------------------------
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
    # -- synthetic parents / verdicts -----------------------------------------------------------
    ("kanban_sync_compat.py", "rejected_parent", "INSERT", _O): SYNTHETIC_UNKNOWN,
    ("kanban_sync_envelope.py", "parent", "INSERT", _P): SYNTHETIC_UNKNOWN,
    # -- in-place rotation of an existing record --------------------------------------------------
    ("kanban_containment_outbox.py", "strip_lifecycle", "UPDATE", _O): IDENTITY_ROTATION,
    ("kanban_sync_client.py", "push", "UPDATE", _O): IDENTITY_ROTATION,
    # -- exact-wire freeze --------------------------------------------------------------------------
    ("kanban_sync_envelope.py", "outbound", "UPDATE", _O): WIRE_FREEZE,
    # -- bookkeeping -------------------------------------------------------------------------------
    ("kanban_actionability_transport.py", "eligible_pending", "UPDATE", _O): STATE_ONLY,
    ("kanban_actionability_transport.py", "record_result", "UPDATE", _O): STATE_ONLY,
    ("kanban_retain_transport.py", "record_result", "UPDATE", _O): STATE_ONLY,
    ("kanban_sync_status_origin.py", "bind", "UPDATE", _O): STATE_ONLY,
    # -- removal of pending custody --------------------------------------------------------------
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
    # -- a fixture generator, not a production writer (scripts/ is scanned so it cannot be forgotten)
    ("gen_source_receipt_fixture.py", "_running", "DELETE", _O): REMOVAL,
    # -- the guards themselves -------------------------------------------------------------------------
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _O): GUARD_INSTALL,
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _P): GUARD_INSTALL,
}

# Sites whose key legitimately covers several statements; any other key is exactly one statement, so a new
# statement inside an already-listed function fails the gate until it is audited here.
SITE_COUNTS: dict[tuple[str, str, str, str], int] = {
    ("kanban_actionability_transport.py", "record_result", "UPDATE", _O): 2,
    ("kanban_retain_transport.py", "record_result", "UPDATE", _O): 2,
    ("kanban_sync_client.py", "push", "UPDATE", _O): 3,
    ("kanban_sync_divergence.py", "repair_divergence", "INSERT", _O): 2,
}

# Statements whose table is a parameter or loop variable, so they could reach a custody table without naming it.
# (file, function, verb, "<dynamic-table>") -> (disposition, can_reach_custody_tables)
_D = "<dynamic-table>"
DYNAMIC_SITES: dict[tuple[str, str, str, str], tuple[str, bool]] = {
    # Rebuild/salvage copies every table row, provenance columns included, into the replacement Board file:
    # whatever it copies must be treated ambiguous by the capture slice, never carried as 'original'.
    ("kanban_db_recover.py", "_salvage_table", "INSERT", _D): (RECOVERY_AMBIGUOUS, True),
    ("kanban_db_recover.py", "_fill_from_backup", "INSERT", _D): (RECOVERY_AMBIGUOUS, True),
    # Fixed tuples of synced domain tables / own bookkeeping tables; neither custody table is in any of them.
    ("flaky_intake.py", "_initialize", "UPDATE", _D): (NOT_CUSTODY, False),
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
    ("kanban_llm_pacing_history.py", "record", "DELETE", _D): (NOT_CUSTODY, False),
    ("kanban_scratch_custody_discovery.py", "discover", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_sync_quarantine.py", "adopt", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_sync_quarantine.py", "restore", "INSERT", _D): (NOT_CUSTODY, False),
    ("kanban_worker_node.py", "_evict_one", "DELETE", _D): (NOT_CUSTODY, False),
}


# Fail-closed file-level pins (see tests/hermes_cli/test_kanban_sync_capture_sites.py). They count raw source text, so
# they do not depend on statement shape, quoting, case, verb (DROP/ALTER included) or directory: any change to a count
# means a pending-custody mutation may have been added, moved or removed and must be audited against SITES above.
# NAME_MENTIONS: case-insensitive mentions of either custody table name per tracked non-test Python file.
NAME_MENTIONS: dict[str, int] = {
    "gateway/kanban_watchers.py": 2,
    "hermes_cli/kanban_actionability_transport.py": 7,
    "hermes_cli/kanban_board_delivery.py": 1,
    "hermes_cli/kanban_containment_outbox.py": 4,
    "hermes_cli/kanban_db_recover.py": 1,
    "hermes_cli/kanban_db_sync.py": 14,
    "hermes_cli/kanban_failure_release.py": 1,
    "hermes_cli/kanban_lanes_sources.py": 1,
    "hermes_cli/kanban_operator_call.py": 1,
    "hermes_cli/kanban_owner_archive_relay.py": 1,
    "hermes_cli/kanban_placement_recovery.py": 2,
    "hermes_cli/kanban_priority_cli.py": 1,
    "hermes_cli/kanban_remote_stop.py": 2,
    "hermes_cli/kanban_replica_freshness.py": 1,
    "hermes_cli/kanban_retain_safety.py": 1,
    "hermes_cli/kanban_retain_transport.py": 5,
    "hermes_cli/kanban_run_end_lease.py": 1,
    "hermes_cli/kanban_sync_backlog.py": 1,
    "hermes_cli/kanban_sync_capture_storage.py": 6,
    "hermes_cli/kanban_sync_client.py": 19,
    "hermes_cli/kanban_sync_compat.py": 5,
    "hermes_cli/kanban_sync_dependency_delete.py": 3,
    "hermes_cli/kanban_sync_divergence.py": 2,
    "hermes_cli/kanban_sync_envelope.py": 6,
    "hermes_cli/kanban_sync_host_outage.py": 1,
    "hermes_cli/kanban_sync_omissions.py": 4,
    "hermes_cli/kanban_sync_outbox_age.py": 5,
    "hermes_cli/kanban_sync_parent_order.py": 1,
    "hermes_cli/kanban_sync_partition_repair.py": 2,
    "hermes_cli/kanban_sync_pending.py": 1,
    "hermes_cli/kanban_sync_quarantine.py": 1,
    "hermes_cli/kanban_sync_rejections.py": 5,
    "hermes_cli/kanban_sync_repair_revision.py": 1,
    "hermes_cli/kanban_sync_replica_age.py": 4,
    "hermes_cli/kanban_sync_reserved_key.py": 10,
    "hermes_cli/kanban_sync_resync_sweep.py": 3,
    "hermes_cli/kanban_sync_status_origin.py": 5,
    "hermes_cli/kanban_worker_node.py": 1,
    "hermes_cli/kanban_worker_repin.py": 1,
    "hermes_cli/worker_drain_results_gate.py": 4,
    "scripts/gen_source_receipt_fixture.py": 2,
}

# DYNAMIC_STATEMENT_COUNTS: INSERT/REPLACE/DELETE/UPDATE...SET whose table is not a plain literal (interpolated, %-formatted,
# concatenated or placeholder), in kanban*/scripts files or files naming a custody table. Audited by hand: every one
# interpolates a module constant naming the module's own bookkeeping table or a fixed tuple of synced domain tables;
# only kanban_db_recover.py can reach a custody table (RECOVERY_AMBIGUOUS in DYNAMIC_SITES above).
DYNAMIC_STATEMENT_COUNTS: dict[str, int] = {
    "hermes_cli/kanban_activity_revision.py": 3,
    "hermes_cli/kanban_annotation_mutation.py": 1,
    "hermes_cli/kanban_candidate_receipts.py": 5,
    "hermes_cli/kanban_capacity_evidence.py": 1,
    "hermes_cli/kanban_containment_connections.py": 1,
    "hermes_cli/kanban_creator_empty.py": 5,
    "hermes_cli/kanban_db.py": 1,
    "hermes_cli/kanban_db_connect.py": 1,
    "hermes_cli/kanban_db_recover.py": 3,
    "hermes_cli/kanban_db_sync.py": 8,
    "hermes_cli/kanban_dispatch_skip.py": 3,
    "hermes_cli/kanban_execution_source.py": 1,
    "hermes_cli/kanban_fairness_journal.py": 9,
    "hermes_cli/kanban_llm_pacing_history.py": 1,
    "hermes_cli/kanban_local_status_wake.py": 2,
    "hermes_cli/kanban_move_parent_evidence.py": 4,
    "hermes_cli/kanban_notify_revision.py": 2,
    "hermes_cli/kanban_pickup_stall.py": 3,
    "hermes_cli/kanban_queue_reason.py": 2,
    "hermes_cli/kanban_review_artifacts.py": 1,
    "hermes_cli/kanban_review_release.py": 1,
    "hermes_cli/kanban_run_tombstones.py": 1,
    "hermes_cli/kanban_scratch_custody.py": 4,
    "hermes_cli/kanban_scratch_custody_discovery.py": 1,
    "hermes_cli/kanban_session_activity.py": 2,
    "hermes_cli/kanban_session_activity_card_details.py": 1,
    "hermes_cli/kanban_source_receipt.py": 2,
    "hermes_cli/kanban_sync_dependency_delete.py": 1,
    "hermes_cli/kanban_sync_own_writes.py": 1,
    "hermes_cli/kanban_sync_parent_refresh.py": 2,
    "hermes_cli/kanban_sync_quarantine.py": 4,
    "hermes_cli/kanban_sync_quarantine_release.py": 1,
    "hermes_cli/kanban_sync_repair_revision.py": 3,
    "hermes_cli/kanban_sync_status_origin.py": 1,
    "hermes_cli/kanban_sync_writer_gate.py": 1,
    "hermes_cli/kanban_update_reclaim_intent.py": 4,
    "hermes_cli/kanban_worker_node.py": 1,
    "optional-skills/security/godmode/scripts/parseltongue.py": 1,
    "optional-skills/software-development/ast-grep/scripts/ast_grep_helper.py": 1,
    "scripts/ci/flaky_intake.py": 1,
}
