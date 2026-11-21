"""Private managed-runtime entry points; IDs are hints, authenticated records are truth.

No public tool or CLI is registered here. The courier must run these in the exact
receiving profile. Network evidence is read independently of its stdin envelope.
"""
from __future__ import annotations

import re

from hermes_cli.kanban_card_answer import AnswerRefused, apply
from hermes_cli.kanban_card_answer_proof import verify
from hermes_cli.kanban_card_answer_source import recorded
from hermes_cli.kanban_card_answer_transport import Records


def _profile():
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name
    profile = get_active_profile_name()
    if profile == 'custom':
        raise AnswerRefused('local_profile_unknown')
    return profile, get_hermes_home()


def owner(request_id: str, proof_id: str) -> dict:
    """Apply an authenticated exact request only in this profile's own live board."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    records = Records()
    profile, _ = _profile()
    original = records.read(request_id)
    args = original.get('args')
    answer = args.get('answer') if isinstance(args, dict) else None
    if (not isinstance(answer, dict) or answer.get('ownerGateway') != records.gateway
            or answer.get('ownerProfile') != profile):
        raise AnswerRefused('owner_route_mismatch')
    # board_own_db_path ignores a worker's HERMES_KANBAN_DB pin. Resolve only a
    # canonical board, never a supplied file or the hub's replica database.
    board = answer.get('board')
    if not isinstance(board, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', board):
        raise AnswerRefused('owner_board_invalid')
    path = kb.board_own_db_path(board)
    if not path.is_file():
        raise AnswerRefused('owner_board_missing')
    # Validate the original even on receipt replay; source readback may be offline
    # only after the durable owner receipt already exists.
    if (original.get('id') != request_id or original.get('to_gw') != records.gateway
            or original.get('coll') != 'tasks' or original.get('action') != 'card-answer'
            or original.get('key') != f"{profile}/{board}/{answer.get('task')}"):
        raise AnswerRefused('owner_route_mismatch')
    with connect_closing(path) as conn:
        return apply(conn, answer, owner_gateway=records.gateway, owner_profile=profile, board=board, require_primary=True,
                     verify_source=lambda a: verify(a, request_id=request_id, proof_id=proof_id,
                                                   owner_gateway=records.gateway, read_request=records.read))


def source(proof_id: str) -> dict:
    """Prove the exact local operation, never a claimed sourceRef or recorded flag."""
    records = Records()
    profile, home = _profile()
    proof = records.read(proof_id)
    args = proof.get('args')
    if not isinstance(args, dict) or set(args) != {'answer', 'source', 'requestId'}:
        raise AnswerRefused('source_request_invalid')
    identity = args['source']
    answer = args['answer']
    if (not isinstance(identity, dict) or set(identity) != {'gateway', 'profile', 'session'}
            or identity.get('gateway') != records.gateway or identity.get('profile') != profile
            or not isinstance(identity.get('session'), str) or not identity['session']
            or not isinstance(answer, dict)):
        raise AnswerRefused('source_route_mismatch')
    # The same validator verifies both legs before the source operation is read.
    # Only the result/state are substituted, not any server-stamped coordinates.
    result = dict(args, gateway=records.gateway, recorded=True)
    validated = dict(proof, state='done', result=result)
    original = records.read(args['requestId'])
    verify(answer, request_id=args['requestId'], proof_id=proof_id, owner_gateway=answer.get('ownerGateway'),
           read_request=lambda rid: validated if rid == proof_id else original)
    return recorded(home / 'user-steps/actions.sqlite3', source=identity,
                    owner_gateway=answer['ownerGateway'], request_id=args['requestId'], answer=answer)
