"""Every ``sessions.model_config`` key the agent writes through ``patch_session_model_config`` must be one a
managed worker may write (``SIDECAR_KEYS``): a missing key makes the owner refuse ``session.sidecars`` with
``invalid_params`` and the managed worker dies (a class fallback's ``model_routing`` write did exactly that).

The scan is static (AST) over the whole tree, so a new writer cannot drift silently: a key it writes must
resolve to a string literal or a module constant, and that string must be registered.
"""
from __future__ import annotations

import ast
from pathlib import Path

from hermes_state_sidecar_keys import OWNER_RESERVED_KEYS, SIDECAR_KEYS

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"tests", "node_modules", "apps", "website", ".git", "venv", ".venv", "build", "dist", ".worktrees"}

# Writers that run only in an owner process (never through a worker RuntimeSessionStore), with the reason.
OWNER_ONLY_WRITERS = {
    # Interactive CLI ``/model``: HermesCLI owns a SessionDB directly; no managed worker runs this mixin.
    "hermes_cli/cli_model_switch_mixin.py",
    # The store implementations themselves (their own forwarding calls take a caller's dict).
    "agent/runtime_session_store.py",
    "hermes_state_sessions.py",
}


def _py_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if not rel.parts or rel.parts[0] in SKIP_DIRS or any(p.startswith(".") for p in rel.parts[:-1]):
            continue
        yield rel, path


def _module_constants(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _writes(tree):
    """Yield (lineno, dict_node_or_None, key_node_or_None) for each model_config write call in *tree*."""
    patchers = set()
    for node in ast.walk(tree):
        # ``patcher = getattr(db, "patch_session_model_config", None)``
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "getattr" \
                and len(node.value.args) >= 2 and isinstance(node.value.args[1], ast.Constant) \
                and node.value.args[1].value == "patch_session_model_config":
            patchers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name == "patch_session_model_config" or name in patchers:
            yield node.lineno, node.args[1] if len(node.args) > 1 else None, None
        elif name == "update_conversation_model_config_value":
            yield node.lineno, None, node.args[1] if len(node.args) > 1 else None
        elif name == "_durable_write" and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == "patch_session_model_config":
            yield node.lineno, node.args[2] if len(node.args) > 2 else None, None
        elif name == "_call_guarded" and node.args and isinstance(node.args[0], ast.Attribute) \
                and node.args[0].attr == "patch_session_model_config":
            argtuple = next((k.value for k in node.keywords if k.arg == "args"), None)
            yield node.lineno, argtuple.elts[1] if isinstance(argtuple, ast.Tuple) and len(argtuple.elts) > 1 else None, None


def _key_ref(key):
    """Reduce a key AST node to ("str", value), ("ident", name) or None (unresolvable), so no tree is retained."""
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return ("str", key.value)
    ident = key.id if isinstance(key, ast.Name) else key.attr if isinstance(key, ast.Attribute) else None
    return ("ident", ident) if ident is not None else None


def _collect():
    # Parse one file at a time and keep only its constants and reduced write records. Holding every tree at once
    # peaked at ~1.6 GB RSS on the self-hosted macOS runner, and each run tipped the shared 16 GB mini into
    # memory-pressure "warn" (hermes kanban t_01a19beb37e87be6).
    constants: dict[Path, dict[str, str]] = {}
    writes: list[tuple[Path, str, list | None]] = []
    for rel, path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(rel))
        constants[rel] = _module_constants(tree)
        if rel.as_posix() in OWNER_ONLY_WRITERS:
            continue
        for lineno, dict_node, key_node in _writes(tree):
            keys = [key_node] if key_node is not None else (
                dict_node.keys if isinstance(dict_node, ast.Dict) else None)
            # ``None`` = non-literal dict or ``**spread``.
            refs = None if keys is None or any(k is None for k in keys) else [_key_ref(k) for k in keys]
            writes.append((rel, f"{rel.as_posix()}:{lineno}", refs))
        del tree
    global_constants: dict[str, set[str]] = {}
    for consts in constants.values():
        for k, v in consts.items():
            global_constants.setdefault(k, set()).add(v)

    def resolve(rel, ref):
        if ref is None:
            return None
        kind, value = ref
        if kind == "str":
            return {value}
        if value in constants[rel]:
            return {constants[rel][value]}
        return global_constants.get(value)

    found, unresolved = {}, []
    for rel, where, refs in writes:
        if refs is None:
            unresolved.append(where)
            continue
        for ref in refs:
            values = resolve(rel, ref)
            if not values:
                unresolved.append(where)
            for value in values or ():
                found.setdefault(value, []).append(where)
    return found, unresolved


def test_every_model_config_writer_key_is_a_worker_sidecar_key():
    found, unresolved = _collect()
    # The scan is live: it must see the known writers, or a refactor silently blinded it.
    assert {"model_routing", "_usage_anchor", "_proactive_prune_rearm_tokens"} <= set(found), sorted(found)
    assert not unresolved, f"model_config writes whose keys the guard cannot resolve: {unresolved}"
    missing = {k: v for k, v in found.items() if k not in SIDECAR_KEYS}
    assert not missing, f"keys written via patch_session_model_config but refused by session.sidecars: {missing}"


def test_owner_reserved_lineage_keys_stay_out_of_the_worker_set():
    assert not (OWNER_RESERVED_KEYS & SIDECAR_KEYS)
    assert {"_delegate_from", "_branched_from"} <= OWNER_RESERVED_KEYS


def test_owner_validator_and_compaction_patch_share_the_registry():
    import hermes_state_worker_context
    assert hermes_state_worker_context.SIDECAR_KEYS is SIDECAR_KEYS
