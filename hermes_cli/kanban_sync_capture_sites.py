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

DISPOSITIONS = frozenset({
    ORIGINAL_CANDIDATE, RECOVERY_AMBIGUOUS, SYNTHETIC_UNKNOWN, IDENTITY_ROTATION,
    WIRE_FREEZE, STATE_ONLY, REMOVAL, GUARD_INSTALL,
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
    # -- the guards themselves -------------------------------------------------------------------------
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _O): GUARD_INSTALL,
    ("kanban_sync_capture_storage.py", "install", "UPDATE", _P): GUARD_INSTALL,
}
