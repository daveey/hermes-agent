"""Shared Kanban placement test helpers: the default route and ``decide`` with fake probes. [daveey]

Imported by test_kanban_placement.py and its split-out sibling test files, so no test module
depends on another test module's internals.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_placement as kp

ROUTE = {"route": {"macbook": "hub"}, "creator_session": {"hub": "20261023_052109_5b3dee"}}
ALIVE = lambda target: None
NOWHERE = lambda need: {"need": need, "local": "absent", "holders": [], "stale": []}
# The target advertises nothing (an older hub client/core): profiles unknown, profile tools unpublished.
SILENT = lambda target: {"profiles": None, "tools": None}


def seen_now(machines):
    """Registry entries stamped as seen just now: label_machines counts only members seen within
    kanban_placement_coordination.FRESH_S. Call it inside a registry fetch so every read is fresh."""
    now = int(time.time() * 1000)
    return [{**m, "last_seen": now} for m in machines]


def decide(args, **kw):
    kw.setdefault("cfg", ROUTE)
    kw.setdefault("self_gw", "macbook")
    kw.setdefault("live", ALIVE)
    kw.setdefault("locate", NOWHERE)
    kw.setdefault("info", SILENT)
    kw.setdefault("carries", False)  # a hermes-hub client without create-task --option (< 1.8.0)
    return kp.decide(args, **kw)
