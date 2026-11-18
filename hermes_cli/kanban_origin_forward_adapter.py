"""Trusted local courier adapter for origin-gateway forwarding admission.

The courier must queue the returned frozen envelope as itself and wait for the
owner's completed origin receipt. Preparing an envelope is NOT a handoff.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from hermes_cli.kanban_origin_adapter import _gateway, _resolve_session
from hermes_cli.kanban_origin_forward import read_lease
from hermes_cli.kanban_origin_forward_decision import prepare


def receive(request):
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_sync_client import Hub, load_identity

    home = get_hermes_home()
    profile = get_active_profile_name()
    if profile == "custom":
        raise ValueError("origin_profile_unknown")
    replica = Path(os.environ.get("HERMES_HUB_STATE") or Path.home() / ".local/state/hermes-hub") / "hub.db"
    gateway = _gateway(replica)

    def lookup(board, task):
        # Construct lazily: durable replay needs neither a live lease nor network.
        return read_lease(Hub(load_identity()), board, task)

    from hermes_cli.kanban_origin_native import lookup as native_lookup
    outbound = prepare(request, path=home / "kanban/origin-forward.db",
                       native_lookup=lambda origin, creator: native_lookup(origin, creator, gateway=gateway),
                       ledger=home / "kanban/origin-ledger.db", gateway=gateway, profile=profile,
                       inventory_lookup=lambda: Hub(load_identity()).machines(),
                       lease_lookup=lookup, now_ms=lambda: int(time.time() * 1000),
                       session_exists=lambda sid: bool(_resolve_session(home / "state.db", sid)))
    return {"status": "prepared", "handoff_complete": False, "outbound": outbound}


def command(args):
    from hermes_cli.kanban_sync_client import HubError
    try:
        raw = sys.stdin.read(262145) if args.request_file == "-" else Path(args.request_file).read_text(encoding="utf-8-sig")
        if len(raw.encode("utf-8")) > 262144:
            raise ValueError("origin_forward_request_invalid")
        result = receive(json.loads(raw))
    except (OSError, sqlite3.Error, HubError):
        result = {"status": "pending", "code": "origin_forward_evidence_unavailable", "handoff_complete": False}
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        code = str(exc)
        if not re.fullmatch(r"origin_[a-z_]+", code):
            code = "origin_forward_request_invalid"
        # Incomplete snapshots are absence of evidence, not a terminal decision.
        status = "pending" if code in {"origin_forward_lease_unavailable", "origin_forward_inventory_unavailable"} else "rejected"
        result = {"status": status, "code": code, "handoff_complete": False}
    print(json.dumps(result))
    return 0
