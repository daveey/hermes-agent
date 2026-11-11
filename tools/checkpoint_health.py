"""Maintenance receipts for the protected one-snapshot-per-project floor."""
import json
from pathlib import Path

from tools.checkpoint_manager import _REFS_PREFIX, _dir_size_bytes, _run_git
from tools.checkpoint_pruning import GC_PENDING_NAME

SUCCESS_MARKER = ".last_prune_success"
FLOOR_MARKER = ".protected_floor"


def _refs(store: Path) -> str | None:
    ok, refs, _ = _run_git(["for-each-ref", "--format=%(refname) %(objectname)"],
                           store, str(store.parent))
    if not ok or not refs.strip():
        return None
    if any(not line.startswith(_REFS_PREFIX) for line in refs.splitlines()):
        return None
    return refs


def record_floor(store: Path) -> bool:
    """Called under the maintenance lock, after successful reclaim and exhausted trimming."""
    if (store / GC_PENDING_NAME).exists():
        return False
    refs = _refs(store)
    if refs is None:
        return False
    ok, parents, _ = _run_git(["rev-list", "--all", "--min-parents=1", "--max-count=1"],
                              store, str(store.parent))
    if not ok or parents.strip():
        return False
    receipt = {"refs": refs, "size": _dir_size_bytes(store)}
    (store.parent / FLOOR_MARKER).write_text(json.dumps(receipt), encoding="utf-8")
    return True


def protected_floor(store: Path, size: int) -> bool:
    """Fail closed when history, objects, or pending work changed since compaction."""
    if (store / GC_PENDING_NAME).exists():
        return False
    try:
        receipt = json.loads((store.parent / FLOOR_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(receipt, dict) or receipt.get("size") != size:
        return False
    refs = _refs(store)
    return refs is not None and receipt.get("refs") == refs
