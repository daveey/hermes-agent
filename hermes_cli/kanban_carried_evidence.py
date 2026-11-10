"""Narrow placement exemption for hash-pinned inputs verified on every worker.

The publisher writes this receipt only after remote installation and readback.
It is not a general exemption for the evidence or workspace directories.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from hermes_cli.config import get_hermes_home

MAX_BYTES = 1024 * 1024
MAX_AGE = 3600
PATH_TOKEN = re.compile(r"(?<![\w/])(?:/home/[^/\s`\"']+|~)/\.hermes/kanban/(?:evidence|workspaces)/[^\s`\"'<>]+")


def without_carried_inputs(text: str, cfg: dict) -> str:
    """Remove only complete, locally intact file tokens with fresh fleet receipts.

    All configured pool/heavy-route destinations must be covered, so adding a
    worker cannot silently make the exemption apply to an unprovisioned host.
    The card must itself pin the matching SHA256. Other markers stay visible.
    """
    pools = cfg.get("worker_pools") or {}
    routes = cfg.get("heavy_route") or {}
    if not isinstance(pools, dict) or not isinstance(routes, dict):
        return text
    destinations = set()
    for members in pools.values():
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            return text
        destinations.update(members)
    if not all(isinstance(m, str) for m in routes.values()):
        return text
    destinations.update(routes.values())
    if not destinations:
        return text
    try:
        path = get_hermes_home() / "kanban" / "carried-evidence.json"
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return text
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("version") != 1:
            return text
        at = receipt.get("verified_at")
        if not isinstance(at, (int, float)) or not 0 <= time.time() - at <= MAX_AGE:
            return text
        machines = receipt.get("machines")
        if not isinstance(machines, list) or not all(isinstance(m, str) for m in machines):
            return text
        if not destinations.issubset(machines):
            return text
        files = receipt.get("files")
        if not isinstance(files, dict):
            return text
    except (OSError, ValueError):
        return text

    def replace(match: re.Match) -> str:
        token = match.group(0)
        # Prose delimiters are not part of a path; descendants/suffixes are.
        name = token.rstrip(".,;:)")
        digest = files.get(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return token
        if not re.search(r"(?<![0-9a-f])" + digest + r"(?![0-9a-f])", text):
            return token
        local = Path(name).expanduser()
        try:
            if ".." in local.parts or local.is_symlink() or local.resolve() != local:
                return token
            if not local.is_file() or local.stat().st_size > MAX_BYTES:
                return token
            if hashlib.sha256(local.read_bytes()).hexdigest() != digest:
                return token
        except OSError:
            return token
        return "[carried input]" + token[len(name):]

    return PATH_TOKEN.sub(replace, text)
