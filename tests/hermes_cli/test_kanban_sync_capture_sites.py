"""Gate: every pending-custody mutation site is audited (POST-COLLECTION-DESIGN section 8). [daveey]

Static scan only: a new, moved or re-verbed INSERT/UPDATE/DELETE/REPLACE against the outbox or parent-
envelope table must be classified in ``kanban_sync_capture_sites.SITES`` before it can land. Nothing here
certifies a capture or activates replay fencing.
"""
from __future__ import annotations

import ast
import pathlib
import re

from hermes_cli import kanban_sync_capture_sites as inv

REPO = pathlib.Path(__file__).resolve().parents[2]
SCAN_DIRS = ("hermes_cli", "gateway", "plugins", "tools", "agent", "cron", "acp_adapter")
PAT = re.compile(
    r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+"
    r"(kanban_sync_outbox|kanban_sync_parent_envelopes)\b", re.IGNORECASE)


def _files():
    for d in SCAN_DIRS:
        base = REPO / d
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))
    yield from sorted(REPO.glob("*.py"))


def _scan() -> dict[tuple[str, str, str, str], list[str]]:
    found: dict[tuple[str, str, str, str], list[str]] = {}
    for path in _files():
        rel = path.relative_to(REPO)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(path.read_text())

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner == "<module>":
                    visit(child, child.name)
                    continue
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    text = " ".join(child.value.split())
                    for m in PAT.finditer(text):
                        verb = m.group(1).split()[0].upper()
                        verb = "INSERT" if verb == "REPLACE" else verb
                        found.setdefault((path.name, owner, verb, m.group(2).lower()), []).append(text[m.start():])
                visit(child, owner)

        visit(tree, "<module>")
    return found


def test_inventory_is_complete_in_both_directions():
    found = set(_scan())
    declared = set(inv.SITES)
    assert found - declared == set(), f"unaudited pending-custody mutation sites: {sorted(found - declared)}"
    assert declared - found == set(), f"stale inventory entries without a source site: {sorted(declared - found)}"


def test_every_disposition_is_known():
    assert set(inv.SITES.values()) <= inv.DISPOSITIONS


def test_no_two_modules_share_a_filename_so_site_keys_are_unambiguous():
    names = [p.name for p in _files() if p.relative_to(REPO).parts[0] != "tests"]
    dup = {n for n in names if names.count(n) > 1}
    scanned = {k[0] for k in _scan()}
    assert not (dup & scanned), f"ambiguous module names carrying sites: {sorted(dup & scanned)}"


def test_only_inserts_may_be_original_or_synthetic_candidates():
    for (_, _, verb, _), disposition in inv.SITES.items():
        if disposition in (inv.ORIGINAL_CANDIDATE, inv.RECOVERY_AMBIGUOUS, inv.SYNTHETIC_UNKNOWN):
            assert verb == "INSERT"
        if disposition == inv.REMOVAL:
            assert verb == "DELETE"
        if disposition in (inv.IDENTITY_ROTATION, inv.WIRE_FREEZE, inv.STATE_ONLY, inv.GUARD_INSTALL):
            assert verb == "UPDATE"


def test_in_place_identity_mutation_is_classified_as_rotation():
    # Any UPDATE assigning op_id/created_at/fields/payload/wire may change an exposed identity: it must
    # be IDENTITY_ROTATION (taint sticky ambiguous) or WIRE_FREEZE; never bookkeeping.
    setter = re.compile(r"\bSET\b(.*?)(?:\bWHERE\b|$)", re.IGNORECASE)
    identity = re.compile(
        r"(?:^|,)\s*(op_id|created_at|fields|wire|wire_json|hlc|task_id|table_name|row_uid|op|base_version)\s*=", re.IGNORECASE)
    for key, statements in _scan().items():
        if key[2] != "UPDATE" or key[1] == "install":  # the guard installer only taints
            continue
        for text in statements:
            assigned = setter.search(text)
            assert assigned, (key, text)
            if identity.search(assigned.group(1)):
                assert inv.SITES[key] in (inv.IDENTITY_ROTATION, inv.WIRE_FREEZE), (key, text)


def test_nothing_in_production_stamps_original_yet():
    """The substrate and this inventory certify nothing: no production module may write 'original'."""
    stamped = []
    for path in _files():
        rel = path.relative_to(REPO)
        if rel.parts[0] == "tests" or path.name in ("kanban_sync_capture_sites.py",):
            continue
        text = path.read_text()
        if re.search(r"identity_state\s*=\s*'original'|capture_protocol\s*=\s*'", text):
            stamped.append(str(rel))
    assert stamped == [], f"original stamping must wait for the audited capture slice: {stamped}"
