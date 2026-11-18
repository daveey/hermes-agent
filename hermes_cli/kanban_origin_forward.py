"""Origin-local admission for worker-delegated creation.

Transport must supply the authenticated sender and a fresh authoritative Board
lease, never a synced task/run row. This module does not send or enqueue work.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_cli.kanban_origin_ledger import _binding_key, binding_hash, validate_binding


def _local_binding(ledger: Path, origin: dict, creator: dict) -> dict:
    # lstat distinguishes an absent ledger from unreadable or dangling evidence.
    # Only ENOENT is terminal; permission/I/O errors and failed opens stay pending.
    try:
        ledger.lstat()
    except FileNotFoundError as exc:
        raise ValueError("origin_forward_binding_missing") from exc
    # Do not initialize an empty ledger and mistake it for authenticated custody.
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT binding_id,binding_hash,binding_json FROM origin_obligations WHERE released=0")
        for key, digest, raw in rows:
            binding = json.loads(raw)
            validate_binding(binding)
            if (binding["owner"] == creator and binding["origin"] == origin
                    and key == _binding_key(binding) and digest == binding_hash(binding)):
                return binding
    raise ValueError("origin_forward_binding_missing")


def read_lease(hub, board: str, task_id: str) -> dict | None:
    """Read the server-owned lease table, bounded and without replica fallback.

    A truncated or malformed response is unavailable evidence, never absence.
    The caller must check expiry against its clock after this network read.
    """
    after = ""
    for _ in range(64):
        page = hub.snapshot_page(board, "leases", after=after)
        if (not isinstance(page, dict) or page.get("table") != "leases"
                or not isinstance(page.get("rows"), list) or type(page.get("more")) is not bool):
            raise ValueError("origin_forward_lease_unavailable")
        previous = after
        found = None
        for row in page["rows"]:
            key = row.get("task_id") if isinstance(row, dict) else None
            if not isinstance(key, str) or key <= previous:
                raise ValueError("origin_forward_lease_unavailable")
            previous = key
            if key == task_id:
                found = row
        if found is not None:
            return found
        if page["more"] is False:
            return None
        if previous == after or page.get("next") != previous:
            raise ValueError("origin_forward_lease_unavailable")
        after = previous
    raise ValueError("origin_forward_lease_unavailable")


def authorize(*, ledger: Path, origin: dict, creator: dict, gateway: str,
              profile: str, sender: str, run_uid: str, lease: dict,
              now_ms: int, session_exists, native_lookup=None) -> dict:
    """Return an accepted local binding, or refuse without modifying any store.

    Authority is admission-time: the transport must freeze a successful decision
    with the immutable outbound envelope before sending. Replays of that durable
    decision do not require a still-live lease; new decisions always do.
    """
    validate_binding({"binding_id": "validation", "version": 1, "publication_version": 1,
                      "origin": origin, "owner": creator,
                      "task_record_key": f"{creator['profile']}/{creator['board']}/{creator['task_id']}"})
    if (origin["gateway"] != gateway or origin["profile"] != profile
            or not isinstance(sender, str) or not re.fullmatch(r"gw:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", sender)
            or not isinstance(run_uid, str) or not run_uid
            or not session_exists(origin["session"])):
        raise ValueError("origin_forward_identity_invalid")
    binding = native_lookup(origin, creator) if native_lookup is not None else None
    if binding is None:
        binding = _local_binding(ledger, origin, creator)
    else:
        validate_binding(binding)
        if binding["origin"] != origin or binding["owner"] != creator or creator["gateway"] != gateway:
            raise ValueError("origin_forward_binding_missing")
    if (not isinstance(lease, dict) or lease.get("task_id") != creator["task_id"]
            or lease.get("holder") != sender[3:] or lease.get("run_uid") != run_uid
            or type(lease.get("expires")) is not int or lease["expires"] <= now_ms):
        raise ValueError("origin_forward_lease_invalid")
    return binding
