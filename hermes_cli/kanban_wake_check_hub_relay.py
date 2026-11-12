"""[daveey] A copy of a hub card parked elsewhere with a probe reading the board relays that probe to the hub.

A park stamps ``probe_host`` = the parking gateway (``kanban_wake_check.park_fields``), so the gateway that parks
runs the probe. A gateway that re-parks a card copied from the hub (``tasks.created_on`` = hub, a Board worker node
running hub work) with a probe that reads a kanban board database (``~/.hermes/kanban.db``, a board under
``~/.hermes/kanban/boards``, ``$HERMES_KANBAN_DB``) would read its OWN board, where the hub's producer rows never
appear: the probe exits 1 ("not yet") forever and nothing says so (t_01a1cd7f37cfb8a7 watched worker-2's board
for hub-created FIX cards).

:func:`relay_host` names the hub as ``probe_host`` for such a park, so the parking gateway registers no probe (and
skips the park-time dry run, which would read the wrong board) and the hub's ``restore_missing_checks`` registers
it from the synced park event: the relay a capability route already uses (``kanban_capability_routing.RELAY_HOME``).

Unchanged: the gateway's own cards; a probe that already runs remotely (``ssh``, ``hub-admin``); a probe that also
reads a fact local to the parking gateway (a workspace or ``wake-checks`` script): the hub cannot run that. Also
unchanged: a copy of a NON-hub card (a Mac's). The hub's board is the board of record for pooled work, so a Mac
card the hub ran with a hub ``kanban.db`` probe meant the hub's board (t_b2390590 waited on hub cards); relaying it
to the Mac would turn a working probe into a silent one.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

HOME_BOARD_RE = re.compile(r"kanban\.db\b|\.hermes/kanban/(?:boards|sync-shadow)\b|\bHERMES_KANBAN_DB\b")
REMOTE_RE = re.compile(r"(?<![\w.-])(?:ssh|hub-admin)(?![\w.-])")
LOCAL_FACT_RE = re.compile(r"/kanban/(?:boards/[^/\s'\"]+/)?(?:workspaces|wake-checks)/|\bHERMES_KANBAN_WORKSPACE\b")


def reads_home_board(cmd: Optional[str]) -> bool:
    """Whether ``cmd`` reads a kanban board database on the host that runs it, and nothing local to the parker."""
    text = cmd or ""
    return bool(HOME_BOARD_RE.search(text)) and not REMOTE_RE.search(text) and not LOCAL_FACT_RE.search(text)


def relay_host(conn: Optional[sqlite3.Connection], task_id: Optional[str], cmd: Optional[str]) -> Optional[str]:
    """The hub gateway that must run ``cmd`` for a park of ``task_id`` here, else None (this gateway runs it)."""
    if conn is None or not task_id or not reads_home_board(cmd):
        return None
    from hermes_cli import kanban_placement as kp
    from hermes_cli.kanban_capability_routing import card_home
    from hermes_cli.kanban_hub_admission_placement import is_hub

    home = card_home(conn, task_id)
    return home if home and is_hub(home, kp.config()) else None
