"""Frozen receive() from 1330267ab4 for wire-compatibility tests.

The unchanged identity/lineage helpers are shared; the old dispatch and omitted
legacy_alias argument must remain frozen, even when the production adapter changes.
"""
import os
from pathlib import Path

from hermes_cli.kanban_origin_adapter import _gateway, _resolve_session
from hermes_cli.kanban_origin_ledger import accept_binding, validate_binding


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
