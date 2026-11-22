"""Gate: every pending-custody mutation site is audited (POST-COLLECTION-DESIGN section 8). [daveey]

Static scan only: a new, moved, re-verbed or additional INSERT/UPDATE/DELETE/REPLACE against the outbox or
parent-envelope table must be classified in ``kanban_sync_capture_sites`` before it can land. Statements whose
table name is interpolated are resolved through module-level string constants; an interpolation that cannot be
resolved (a parameter such as ``table``) is a ``<dynamic-table>`` site and must be audited explicitly, because it
can reach the custody tables without naming them. Dynamic statements are only scanned in
``kanban*`` modules, ``scripts/`` or that name a custody table. A second, fail-closed layer pins every SQL
write fragment per function in any tracked file that mentions "kanban", whether or not it names a table.
Nothing here certifies a capture or activates replay fencing.

Convention: like the other static guards that read source, this is a deliberate source-reading gate (it is the
only way to prove an inventory is complete); it is cached so the tree is parsed once.
"""
from __future__ import annotations

import ast
import functools
import pathlib
import re

from hermes_cli import kanban_sync_capture_sites as inv

REPO = pathlib.Path(__file__).resolve().parents[2]
SCAN_DIRS = ("hermes_cli", "gateway", "plugins", "tools", "agent", "cron", "acp_adapter", "scripts")
CUSTODY = ("kanban_sync_outbox", "kanban_sync_parent_envelopes")
VERB = r"(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+"
PAT = re.compile(r"\b" + VERB + r"(kanban_sync_outbox|kanban_sync_parent_envelopes)\b", re.IGNORECASE)
# Any verb whose table is an unresolved interpolation / concatenation / placeholder (SQL is upper case here;
# UPDATE must be followed by SET so log text such as "update {x}: ..." is not mistaken for SQL).
DYN = re.compile(
    r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM)\s+(?:\{\?\}|%s)"
    r"|\b(UPDATE(?:\s+OR\s+\w+)?)\s+(?:\{\?\}|%s)\s+SET\b")
DYNAMIC = "<dynamic-table>"


def _files():
    for d in SCAN_DIRS:
        base = REPO / d
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))
    yield from sorted(REPO.glob("*.py"))


def _production():
    for path in _files():
        rel = path.relative_to(REPO)
        if rel.parts[0] != "tests" and "__pycache__" not in rel.parts:
            yield path, rel


def _constants(tree) -> dict[str, str]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):  # A, B = "a", "b"
            for target in node.targets:
                if isinstance(target, ast.Tuple) and len(target.elts) == len(node.value.elts):
                    for name, value in zip(target.elts, node.value.elts):
                        if isinstance(name, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
                            out[name.id] = value.value
    return out


@functools.cache
def _scan() -> dict[tuple[str, str, str, str], list[str]]:
    parsed = {}
    for path, _ in _production():
        text = path.read_text()
        if "kanban" not in text.lower() and "scripts" not in path.parts:  # Board DB is only reached via kanban modules
            continue
        tree = ast.parse(text)
        in_scope = path.name.startswith("kanban") or "scripts" in path.parts or any(t in text for t in CUSTODY)
        parsed[path] = (tree, _constants(tree), in_scope)
    global_consts: dict[str, set[str]] = {}
    for _, consts, _ in parsed.values():
        for name, value in consts.items():
            global_consts.setdefault(name, set()).add(value)

    def resolve(expr, local):
        name = expr.id if isinstance(expr, ast.Name) else expr.attr if isinstance(expr, ast.Attribute) else None
        if name is None:
            return None
        if name in local:
            return local[name]
        values = global_consts.get(name, set())
        return next(iter(values)) if len(values) == 1 else None

    found: dict[tuple[str, str, str, str], list[str]] = {}
    for path, (tree, local, mentions_kanban) in parsed.items():

        def record(owner, text, dynamic_ok):
            text = " ".join(text.split())
            for m in PAT.finditer(text):
                verb = m.group(1).split()[0].upper()
                verb = "INSERT" if verb == "REPLACE" else verb
                found.setdefault((path.name, owner, verb, m.group(2).lower()), []).append(text[m.start():])
            if dynamic_ok:
                for m in DYN.finditer(text):
                    verb = (m.group(1) or m.group(2)).split()[0].upper()
                    verb = "INSERT" if verb == "REPLACE" else verb
                    found.setdefault((path.name, owner, verb, DYNAMIC), []).append(text[m.start():])

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner == "<module>":
                    visit(child, child.name)
                    continue
                if isinstance(child, ast.JoinedStr):
                    parts = []
                    for value in child.values:
                        if isinstance(value, ast.Constant):
                            parts.append(str(value.value))
                        else:
                            resolved = resolve(value.value, local) if isinstance(value, ast.FormattedValue) else None
                            parts.append(resolved if resolved is not None else "{?}")
                    record(owner, "".join(parts), mentions_kanban)
                    for value in child.values:
                        if isinstance(value, ast.FormattedValue):
                            visit(value, owner)
                    continue
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    record(owner, child.value, False)
                elif isinstance(child, ast.BinOp) and isinstance(child.op, ast.Add) \
                        and isinstance(child.left, ast.Constant) and isinstance(child.left.value, str):
                    resolved = resolve(child.right, local)
                    record(owner, child.left.value + (resolved if resolved is not None else "{?}"), mentions_kanban)
                visit(child, owner)

        visit(tree, "<module>")
    return found


def test_inventory_is_complete_in_both_directions():
    found = {k for k in _scan() if k[3] != DYNAMIC}
    declared = set(inv.SITES)
    assert found - declared == set(), f"unaudited pending-custody mutation sites: {sorted(found - declared)}"
    assert declared - found == set(), f"stale inventory entries without a source site: {sorted(declared - found)}"


def test_statement_counts_are_pinned_per_site():
    """A new statement inside an already-listed function changes its count and must be re-audited."""
    counts = {key: len(stmts) for key, stmts in _scan().items() if key[3] != DYNAMIC}
    declared = {key: inv.SITE_COUNTS.get(key, 1) for key in inv.SITES}
    assert counts == declared, {k: (counts.get(k), declared.get(k)) for k in set(counts) | set(declared)
                                if counts.get(k) != declared.get(k)}


def test_dynamic_table_statements_are_audited_and_recovery_copies_are_never_original():
    dynamic = {k for k in _scan() if k[3] == DYNAMIC}
    assert dynamic == set(inv.DYNAMIC_SITES), sorted(dynamic ^ set(inv.DYNAMIC_SITES))
    for key, (disposition, reaches_custody) in inv.DYNAMIC_SITES.items():
        assert disposition in inv.DISPOSITIONS, key
        if reaches_custody:  # can write a custody table: a copy must never carry or mint 'original'
            assert disposition == inv.RECOVERY_AMBIGUOUS, key


def test_every_disposition_is_known():
    assert set(inv.SITES.values()) <= inv.DISPOSITIONS


def test_no_two_modules_share_a_filename_so_site_keys_are_unambiguous():
    names = [p.name for p, _ in _production()]
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
        if key[2] != "UPDATE" or key[1] == "install" or key[3] == DYNAMIC:  # the guard installer only taints
            continue
        for text in statements:
            assigned = setter.search(text)
            assert assigned, (key, text)
            if identity.search(assigned.group(1)):
                assert inv.SITES[key] in (inv.IDENTITY_ROTATION, inv.WIRE_FREEZE), (key, text)


def test_nothing_in_production_stamps_original_yet():
    """The substrate and this inventory certify nothing: no production module may write 'original'."""
    stamped = []
    for path, rel in _production():
        if path.name in ("kanban_sync_capture_sites.py",):
            continue
        text = path.read_text()
        if re.search(r"identity_state\s*=\s*'original'|capture_protocol\s*=\s*'", text):
            stamped.append(str(rel))
    assert stamped == [], f"original stamping must wait for the audited capture slice: {stamped}"


# --- fail-closed file-level layer: independent of statement shape, quoting, case, verb or directory ---------
# A custody table name as a whole identifier: the modules kanban_sync_outbox_age/_observation are not mentions.
ANY_NAME = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(CUSTODY) + r")(?![A-Za-z0-9_])", re.IGNORECASE)
# Any fragment of a SQL write / schema change / schema read inside a string constant. Fragments are matched
# one string constant at a time, so concatenated, %-formatted, f-string, ``+=`` and join-built statements are
# still counted; none of them has to name a table. It is only ever applied to files that mention "kanban".
FRAG = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM|DROP\s+(?:TABLE|TRIGGER|INDEX|VIEW)"
    r"|ALTER\s+TABLE|UPDATE(?:\s+OR\s+\w+)?\s+[^\s;]{1,80}\s+SET"
    r"|(?:INSERT|REPLACE|DELETE|UPDATE)(?:\s+OR\s+\w+)?\s*$|writable_schema|sqlite_(?:master|schema))", re.IGNORECASE)


@functools.cache
def _tracked_python():
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=REPO, capture_output=True, check=True).stdout
    return sorted(p for p in out.decode().split("\0") if p and not p.startswith("tests/") and p != "hermes_cli/kanban_sync_capture_sites.py")


def _owner(stack):
    return ".".join(stack[:2]) or "<module>"


def _fragments(tree):
    counts: dict[str, int] = {}

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child.name] if len(stack) < 2 else stack)
                continue
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                n = len(FRAG.findall(child.value))
                if n:
                    counts[_owner(stack)] = counts.get(_owner(stack), 0) + n
            visit(child, stack)

    visit(tree, [])
    return counts


@functools.cache
def _file_counts():
    names, frags = {}, {}
    for rel in _tracked_python():
        text = (REPO / rel).read_text(errors="replace")
        n = len(ANY_NAME.findall(text))
        if n:
            names[rel] = n
        if "kanban" in text.lower() or ANY_NAME.search(text):
            for owner, count in _fragments(ast.parse(text)).items():
                frags[f"{rel}::{owner}"] = count
    return names, frags


def _diff(a, b):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}


def test_every_mention_of_a_custody_table_name_is_pinned_per_file_anywhere_in_the_tree():
    """Quoted, schema-qualified, lowercase, DROP/ALTER, any directory: a changed mention count must be audited."""
    names, _ = _file_counts()
    assert names == dict(inv.NAME_MENTIONS), _diff(names, inv.NAME_MENTIONS)


def test_every_sql_write_fragment_in_a_kanban_touching_file_is_pinned_per_function():
    """Any write/DDL/schema-read fragment, named or not, in any tracked file that touches kanban, counted per function."""
    _, frags = _file_counts()
    assert frags == dict(inv.WRITE_FRAGMENTS), _diff(frags, inv.WRITE_FRAGMENTS)
