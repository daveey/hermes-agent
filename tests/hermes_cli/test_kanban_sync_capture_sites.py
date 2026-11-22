"""Exact reviewed inventory snapshot; not an automatic write-discovery gate. [daveey]

This pins identities, resolved table sets, verbs and classifications. It does not
prove that an unlisted writer cannot exist or that a listed function still writes
the declared tables. No SQLite interception or source-substring SQL matching.
"""
from __future__ import annotations

import ast
from pathlib import Path

from hermes_cli import kanban_sync_capture_sites as inventory


# Independently reviewed snapshot. Full module + qualified function identities:
# a rename, extra prefix, other module, table, verb or disposition changes the pin.
EXPECTED_SITES = """
hermes_cli.kanban_db_sync._outbox_insert|INSERT|kanban_sync_outbox|original_candidate
hermes_cli.kanban_db_sync._insert_trigger|INSERT|kanban_sync_outbox|original_candidate
hermes_cli.kanban_retain_transport.reserve|INSERT|kanban_sync_outbox|original_candidate
hermes_cli.kanban_actionability_transport.reserve|INSERT|kanban_sync_outbox|original_candidate
hermes_cli.kanban_failure_release.queue|INSERT|kanban_sync_outbox|original_candidate
hermes_cli.kanban_db_sync._queue_rows|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_client.import_board|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_client.repair_missing_children|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_client.retire_lost_placeholder_links|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_compat.enqueue_retry|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_compat._drop_virtual|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_divergence.repair_divergence|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_omissions._queue_restore|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_reserved_key._queue_children|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_reserved_key._write_receipt|INSERT|kanban_sync_outbox|recovery_ambiguous
hermes_cli.kanban_sync_compat.rejected_parent|INSERT|kanban_sync_outbox|synthetic_unknown
hermes_cli.kanban_sync_envelope.parent|INSERT|kanban_sync_parent_envelopes|synthetic_unknown
hermes_cli.kanban_containment_outbox.strip_lifecycle|UPDATE|kanban_sync_outbox|identity_rotation
hermes_cli.kanban_sync_client.push|UPDATE|kanban_sync_outbox|identity_rotation
hermes_cli.kanban_sync_envelope.outbound|UPDATE|kanban_sync_outbox|wire_freeze
hermes_cli.kanban_actionability_transport.eligible_pending|UPDATE|kanban_sync_outbox|state_only
hermes_cli.kanban_actionability_transport.record_result|UPDATE|kanban_sync_outbox|state_only
hermes_cli.kanban_retain_transport.record_result|UPDATE|kanban_sync_outbox|state_only
hermes_cli.kanban_sync_status_origin.bind|UPDATE|kanban_sync_outbox|state_only
hermes_cli.kanban_actionability_transport.confirm|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_actionability_transport.record_result|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_containment_outbox.strip_lifecycle|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_db_sync._apply_one|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_retain_safety.recover|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_retain_transport.confirm|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_retain_transport.record_result|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_client.import_board|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_client.push|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_compat._drop_virtual|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_dependency_delete.settle|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_envelope.prune_parents|DELETE|kanban_sync_parent_envelopes|removal
hermes_cli.kanban_sync_envelope.retire_parent|DELETE|kanban_sync_parent_envelopes|removal
hermes_cli.kanban_sync_rejections.resolve_rejections|DELETE|kanban_sync_outbox|removal
scripts.gen_source_receipt_fixture._running|DELETE|kanban_sync_outbox|removal
hermes_cli.kanban_sync_capture_storage.install|UPDATE|kanban_sync_outbox|guard_install
hermes_cli.kanban_sync_capture_storage.install|UPDATE|kanban_sync_parent_envelopes|guard_install
"""

# Exact resolved custody-table sets for each reviewed dynamic site. Empty means
# reviewed non-custody, not permission to write any table. These are declarations,
# not observations or enforcement. Keep the classifications pinned separately.
EXPECTED_DYNAMIC = {
    ("hermes_cli.kanban_db_recover._salvage_table", "INSERT"): {"kanban_sync_outbox", "kanban_sync_parent_envelopes"},
    ("hermes_cli.kanban_db_recover._fill_from_backup", "INSERT"): {"kanban_sync_outbox", "kanban_sync_parent_envelopes"},
    ("scripts.ci.flaky_intake.Journal._initialize", "UPDATE"): set(),
    ("hermes_cli.kanban_candidate_receipts.persist", "DELETE"): set(),
    ("hermes_cli.kanban_containment_connections.bootstrap", "UPDATE"): set(),
    ("hermes_cli.kanban_db._delete_task_relations", "DELETE"): set(),
    ("hermes_cli.kanban_db_connect._rebuild_drifted_tables", "INSERT"): set(),
    ("hermes_cli.kanban_db_sync._delete_row", "DELETE"): set(),
    ("hermes_cli.kanban_db_sync._insert_trigger", "UPDATE"): set(),
    ("hermes_cli.kanban_db_sync._upsert_row", "INSERT"): set(),
    ("hermes_cli.kanban_db_sync._upsert_row", "UPDATE"): set(),
    ("hermes_cli.kanban_db_sync.install_or_remove", "UPDATE"): set(),
    ("hermes_cli.kanban_db_sync.repair_ms_timestamps", "UPDATE"): set(),
    ("hermes_cli.kanban_llm_pacing_history.History.record", "DELETE"): set(),
    ("hermes_cli.kanban_scratch_custody_discovery.discover", "INSERT"): set(),
    ("hermes_cli.kanban_sync_quarantine.adopt", "INSERT"): set(),
    ("hermes_cli.kanban_sync_quarantine.restore", "INSERT"): set(),
    ("hermes_cli.kanban_worker_node._evict_one", "DELETE"): set(),
}


def qualified(filename, function):
    module = Path(filename).stem
    package = {"gen_source_receipt_fixture.py": "scripts", "flaky_intake.py": "scripts.ci"}.get(filename, "hermes_cli")
    return f"{package}.{module}.{function}"


def test_exact_reviewed_sites():
    expected_rows = [tuple(row.split("|")) for row in EXPECTED_SITES.strip().splitlines()]
    expected = {row[:3]: row[3] for row in expected_rows}
    assert len(expected) == len(expected_rows), "duplicate reviewed entry"
    actual = {
        (qualified(filename, function), verb, table): disposition
        for (filename, function, verb, table), disposition in inventory.SITES.items()
    }
    assert len(actual) == len(inventory.SITES), "qualified identities must not collide"
    assert actual == expected


def test_exact_reviewed_dynamic_table_sets():
    actual = {}
    for (filename, function, verb, table), (disposition, reaches_custody) in inventory.DYNAMIC_SITES.items():
        assert table == "<dynamic-table>"
        assert type(reaches_custody) is bool
        assert disposition == (inventory.RECOVERY_AMBIGUOUS if reaches_custody else inventory.NOT_CUSTODY)
        actual[qualified(filename, function), verb] = (
            {"kanban_sync_outbox", "kanban_sync_parent_envelopes"} if reaches_custody else set()
        )
    assert len(actual) == len(inventory.DYNAMIC_SITES)
    assert actual == EXPECTED_DYNAMIC


def test_reviewed_functions_exist_at_exact_qualified_identity():
    """AST declarations only: no importing writers, SQL discovery or interception."""
    root = Path(__file__).resolve().parents[2]
    entries = set(inventory.SITES) | set(inventory.DYNAMIC_SITES)
    for filename, function, _, _ in sorted(entries):
        module = qualified(filename, "").rstrip(".")
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        nodes = tree.body
        for component in function.split("."):
            matches = [node for node in nodes if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == component]
            assert len(matches) == 1, qualified(filename, function)
            nodes = matches[0].body
        assert isinstance(matches[0], (ast.FunctionDef, ast.AsyncFunctionDef))


def test_dispositions_and_original_candidates_remain_bounded():
    assert set(inventory.SITES.values()) <= inventory.DISPOSITIONS
    for (_, _, verb, _), disposition in inventory.SITES.items():
        if disposition in (inventory.ORIGINAL_CANDIDATE, inventory.RECOVERY_AMBIGUOUS, inventory.SYNTHETIC_UNKNOWN):
            assert verb == "INSERT"
