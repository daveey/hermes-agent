"""Trusted local courier entry for durable origin receipts (no network auth here).

The hub client supplies a server-stamped envelope over stdin. This CLI is not a
public endpoint: possession of local OS access is its trust boundary. Destination
identity comes from local configuration, never from that envelope.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from hermes_cli.kanban_origin import _ro
from hermes_cli.kanban_origin_ledger import accept_binding, validate_binding


def _gateway(replica: Path) -> str:
    cfg = Path(os.environ.get('HERMES_MAIL_CONFIG') or Path.home() / '.config/hermes-mail/config.json')
    config = json.loads(cfg.read_text(encoding="utf-8-sig"))
    gateway = config.get('gateway') if isinstance(config, dict) else None
    if not isinstance(gateway, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', gateway):
        raise ValueError('origin_gateway_invalid')
    if replica.exists():
        db = _ro(replica)
        try:
            row = db.execute("SELECT v FROM meta WHERE k='gateway'").fetchone()
            if not row or row[0] != gateway:
                raise ValueError('origin_gateway_conflict')
        finally:
            db.close()
    return gateway


def _resolve_session(path: Path, session: str) -> str:
    # Use the canonical continuation predicate but refuse cycles/cap exhaustion;
    # the UI convenience get_compression_chain intentionally truncates both.
    from hermes_state_compression import _CHAIN_CAP, _CHAIN_STEP_SQL
    db = _ro(path)
    try:
        db.execute('BEGIN')
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone():
            raise ValueError('origin_session_missing')
        seen = {session}
        for _ in range(_CHAIN_CAP):
            row = db.execute(_CHAIN_STEP_SQL, (session,)).fetchone()
            if row is None:
                return session
            session = row[0]
            if session in seen:
                raise ValueError('origin_lineage_invalid')
            seen.add(session)
        raise ValueError('origin_lineage_invalid')
    finally:
        db.close()


def receive(request: dict) -> dict:
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name
    home = get_hermes_home()
    profile = get_active_profile_name()
    if profile == 'custom':
        raise ValueError('origin_profile_unknown')
    replica = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    gateway = _gateway(replica)
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('origin_request_invalid')
    binding = request['args'].get('binding')
    validate_binding(binding)
    if (request.get('coll') != 'sessions' or request.get('action') != 'origin-bind'
            or request.get('to_gw') != gateway
            or request.get('key') != f"{profile}/{binding['origin']['session']}"):
        raise ValueError('origin_route_mismatch')
    return accept_binding(path=home / 'kanban/origin-ledger.db', replica=replica,
                          binding=binding, request_id=request.get('id'),
                          sender=request.get('from'), gateway=gateway, profile=profile,
                          resolve_session=lambda sid: _resolve_session(home / 'state.db', sid))


def command(args) -> int:
    try:
        raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding="utf-8-sig")
        result = receive(json.loads(raw))
    except (OSError, sqlite3.Error):
        # Availability is not terminal rejection. Do not print paths or private
        # config content into courier results.
        result = {'status': 'pending', 'code': 'origin_storage_unavailable', 'handoff_complete': False}
    except (ValueError, TypeError) as exc:
        code = str(exc)
        if not re.fullmatch(r'origin_[a-z_]+', code):
            code = 'origin_request_invalid'
        result = {'status': 'rejected', 'code': code, 'handoff_complete': False}
    print(json.dumps(result))
    return 0
