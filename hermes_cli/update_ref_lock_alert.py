"""One Kanban card when the same Git ref lock blocks two distinct ``hermes update`` runs.

A single ``ref_lock_stale`` stop may be a live git; the same lock file (canonical path + inode +
mtime) still present after a second, different update run is a stranded lock worth a human look.
This module only DIAGNOSES: it never deletes or authorizes deleting a lock (reclamation stays in
``gitlock.release_dead_fetch_locks`` behind its dead-owner and holder proofs).

Pieces, kept apart on purpose:
  * :func:`lock_identity` reads the failing git output + one ``lstat``; the stop-class classifier
    in ``update_receipt`` stays pure.
  * a small durable ledger (``<receipt dir>/ref_lock_alerts.json``) remembers which distinct
    ``update_id`` s saw which lock identity, so the count survives process exits.
  * publication goes through ``kanban_db.create_task(idempotency_key=...)``, the canonical
    idempotent creator; a publish failure leaves the entry pending and the next finalize retries.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

STOP_CLASS = "ref_lock_stale"
FACT = "ref_lock"
AUTHOR = "update-ref-lock-alert"
KEY_PREFIX = "update-ref-lock-"
MIN_RUNS = 2
_MAX_ENTRIES = 32
_MAX_RUNS = 8
_MAX_AGE_S = 30 * 86400
_LEDGER = "ref_lock_alerts.json"

_PATH = re.compile(r"Unable to create '([^'\n]+\.lock)'")
_REF_LOCK = re.compile(r"(?:^|[/\\])(?:HEAD|packed-refs|refs[/\\].+)\.lock$")


def lock_identity(output: str) -> Optional[dict[str, Any]]:
    """The ref lock named by git's "Unable to create '<path>.lock': File exists" output, or None.

    Path is canonicalized; ``dev``/``ino``/``mtime_ns`` (lstat) distinguish a lock generation and
    are None when the lock is already gone.
    """
    match = _PATH.search(output or "")
    if not match or "File exists" not in output or not _REF_LOCK.search(match.group(1)):
        return None
    raw = match.group(1)
    try:
        path = os.path.realpath(raw)
    except (OSError, ValueError):
        path = os.path.normpath(raw)
    identity: dict[str, Any] = {"path": path, "dev": None, "ino": None, "mtime_ns": None}
    with contextlib.suppress(OSError):
        st = os.lstat(path)
        identity.update(dev=st.st_dev, ino=st.st_ino, mtime_ns=st.st_mtime_ns)
    return identity


def identity_key(identity: dict[str, Any]) -> str:
    material = json.dumps([identity.get("path"), identity.get("dev"), identity.get("ino"),
                           identity.get("mtime_ns")])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _ledger_path(directory: Optional[Path]) -> Path:
    if directory is None:
        from hermes_cli.update_receipt import _receipt_dir
        directory = _receipt_dir()
    return Path(directory) / _LEDGER


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".guard", "ab") as guard:
        try:
            import fcntl
            fcntl.flock(guard, fcntl.LOCK_EX)
        except ImportError as exc:
            # Until a native Windows lock is implemented, skip diagnostics there rather than
            # race read/modify/write or publish from an unprotected ledger.
            raise RuntimeError("ref-lock alert ledger requires fcntl locking") from exc
        yield


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) and isinstance(data.get("entries"), dict) else {"entries": {}}
    except (OSError, ValueError):
        return {"entries": {}}


def _store(path: Path, ledger: dict[str, Any]) -> None:
    from hermes_cli.runtime_state import _atomic_bytes
    _atomic_bytes(path, (json.dumps(ledger, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def publish_card(entry: dict[str, Any], key: str) -> str:
    """Open (or find, by idempotency key) the one card for this lock; returns its task id."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    ident = entry["identity"]
    runs = ", ".join(f"`{r}`" for r in entry["runs"])
    body = (
        f"The same Git ref lock blocked `hermes update` in {len(entry['runs'])} distinct runs: {runs}.\n\n"
        f"Lock: `{ident['path']}` (inode {ident.get('ino')}, mtime_ns {ident.get('mtime_ns')}).\n\n"
        "This is a diagnostic only; nothing was deleted. Confirm no git process holds the lock "
        "(`lsof`/`fuser` on the path) and that the update that created it is dead, then remove it "
        "by hand, or re-run `hermes update`, whose recovery reclaims it only with dead-owner proof."
    )
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        return kb.create_task(
            conn, title=f"Stale git ref lock blocks hermes update: {ident['path']}"[:120], body=body,
            assignee="default", created_by=AUTHOR, priority=50,
            idempotency_key=KEY_PREFIX + key,
        )


def observe(data: dict[str, Any], *, directory: Optional[Path] = None,
            publisher: Optional[Callable[[dict[str, Any], str], str]] = None,
            now: Optional[float] = None) -> list[str]:
    """Record a finalized receipt's ref-lock stop and publish every lock seen by >=2 distinct runs.

    Also retries earlier unpublished entries. Returns the card ids published now. Never raises.
    """
    publisher = publisher or publish_card
    now = time.time() if now is None else now
    published: list[str] = []
    try:
        path = _ledger_path(directory)
        with _locked(path):
            ledger = _load(path)
            before = json.dumps(ledger, sort_keys=True)
            entries = ledger["entries"]
            for k in [k for k, e in entries.items() if now - e.get("seen_at", 0) > _MAX_AGE_S]:
                del entries[k]
            ident, run = data.get(FACT), str(data.get("update_id") or "")
            if (data.get("outcome") == "failed" and data.get("stop_class") == STOP_CLASS
                    and run and isinstance(ident, dict)
                    and ident.get("path")):
                key = identity_key(ident)
                entry = entries.setdefault(key, {"identity": ident, "runs": [], "card": None, "attempts": 0})
                entry["seen_at"] = now
                if run not in entry["runs"]:
                    entry["runs"] = (entry["runs"] + [run])[-_MAX_RUNS:]
            if len(entries) > _MAX_ENTRIES:
                for k in sorted(entries, key=lambda k: entries[k].get("seen_at", 0))[:len(entries) - _MAX_ENTRIES]:
                    del entries[k]
            for key, entry in entries.items():
                if entry.get("card") or len(entry["runs"]) < MIN_RUNS:
                    continue
                try:
                    entry["card"] = publisher(entry, key)
                    published.append(entry["card"])
                except Exception as exc:
                    entry["attempts"] = entry.get("attempts", 0) + 1
                    logger.warning("ref-lock alert card not filed (will retry): %s", exc, exc_info=True)
            if json.dumps(ledger, sort_keys=True) != before:  # every finalize passes here; most change nothing
                _store(path, ledger)
    except Exception:
        logger.warning("ref-lock alert bookkeeping failed", exc_info=True)
    return published
