"""Strict validation of attachment-backed global normalization plans."""
from __future__ import annotations

from hermes_cli.kanban_priority_migration import plan_global


STATUSES = frozenset({"todo", "ready", "running", "review", "blocked", "scheduled", "triage", "done", "archived"})


def validate(manifest: dict) -> dict:
    """Recompute the plan from originals; attachment integrity is not validation."""
    try:
        return _validate(manifest)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid priority manifest: {exc}") from exc


def _validate(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != {
        "version", "cap", "board_id", "ceiling", "exceptions", "rows"
    }:
        raise ValueError("unknown fields")
    if not isinstance(manifest["board_id"], str) or not manifest["board_id"]:
        raise ValueError("missing board identity")
    if any(type(manifest[key]) is not int for key in ("version", "cap", "ceiling")):
        raise ValueError("noninteger policy version")
    if not isinstance(manifest["rows"], list) or not isinstance(manifest["exceptions"], list):
        raise ValueError("rows and exceptions must be lists")
    inventory = []
    for row in manifest["rows"]:
        if not isinstance(row, dict) or set(row) != {"id", "origin", "old", "new"}:
            raise ValueError("invalid row")
        if any(not isinstance(row[k], str) or not row[k] for k in ("id", "origin")):
            raise ValueError("missing row identity")
        if any(type(row[k]) is not int for k in ("old", "new")):
            raise ValueError("noninteger priority")
        inventory.append({"id": row["id"], "created_on": row["origin"],
                          "priority": row["old"], "status": "ready"})
    indexed = {row["id"]: row for row in inventory}
    exceptions = []
    for row in manifest["exceptions"]:
        if not isinstance(row, dict) or set(row) != {"id", "origin", "old", "status"}:
            raise ValueError("invalid exception")
        if any(not isinstance(row[k], str) or not row[k] for k in ("id", "origin", "status")):
            raise ValueError("missing exception identity")
        if type(row["old"]) is not int or row["status"] not in STATUSES:
            raise ValueError("invalid exception value or status")
        original = {"id": row["id"], "created_on": row["origin"],
                    "priority": row["old"], "status": row["status"]}
        if row["status"] in ("done", "archived"):
            if row["id"] in indexed:
                raise ValueError("terminal exception is in open inventory")
            inventory.append(original)
        else:
            active = indexed.get(row["id"])
            if active is None or active["created_on"] != row["origin"] or active["priority"] != row["old"]:
                raise ValueError("exception differs from open inventory")
            active["status"] = row["status"]
        exceptions.append(row["id"])
    expected = plan_global(inventory, board_id=manifest["board_id"], exceptions=exceptions)
    if expected != manifest:
        raise ValueError("plan differs from canonical global ranking")
    return manifest
