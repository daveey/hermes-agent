"""Managed-runtime entry points; IDs are hints, authenticated records are truth.

The only entry is the module CLI (no tool or ``hermes`` subcommand is registered)::

    python -m hermes_cli.kanban_card_answer_runtime owner REQUEST_ID PROOF_ID
    python -m hermes_cli.kanban_card_answer_runtime source PROOF_ID

Run it in the exact receiving profile (``HERMES_HOME``). One JSON object is printed
to stdout. Success: exit 0, ``{"ok": true, "result": {...}}``. Controlled refusal
(``AnswerRefused``; nothing was changed): exit 2, ``{"ok": false, "refused": CODE}``.
Retryable unavailability (courier/store I/O): exit 3,
``{"ok": false, "retryable": true, "error": "unavailable"}``. Any other failure:
exit 1, ``{"ok": false, "error": "internal_error", "type": NAME}``. Bad usage: exit
64. Network evidence is read independently of any caller-supplied envelope; there is
no flag that supplies, skips or overrides proof.

Routing coordinates: ``ownerProfile`` and the courier key ``profile/board/task`` name
the Hermes profile whose runtime executes this transition. They are routing
coordinates, NOT the task's assignee, which is never consulted or changed. Boards
are shared across profiles under the trusted base Hermes root.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

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


def _board_db(board) -> Path:
    """Canonical live board DB under the trusted base Hermes root.

    ``HERMES_KANBAN_HOME`` and ``HERMES_KANBAN_DB`` (worker/test pins) are ignored:
    only ``<base>/kanban.db`` (default) or ``<base>/kanban/boards/<slug>/kanban.db``.
    """
    if not isinstance(board, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', board):
        raise AnswerRefused('owner_board_invalid')
    from hermes_constants import get_default_hermes_root
    root = get_default_hermes_root().resolve()
    if board == 'default':
        path = (root / 'kanban.db').resolve()
        if path.parent != root:
            raise AnswerRefused('owner_board_invalid')
    else:
        path = (root / 'kanban' / 'boards' / board / 'kanban.db').resolve()
        if not path.is_relative_to(root / 'kanban' / 'boards'):  # symlink escaping the root
            raise AnswerRefused('owner_board_invalid')
    if not path.is_file():
        raise AnswerRefused('owner_board_missing')
    return path


def owner(request_id: str, proof_id: str) -> dict:
    """Apply an authenticated exact request only in this profile's own live board."""
    from hermes_cli.kanban_db_connect import connect_closing
    records = Records()
    profile, _ = _profile()
    original = records.read(request_id)
    args = original.get('args')
    answer = args.get('answer') if isinstance(args, dict) else None
    if (not isinstance(answer, dict) or answer.get('ownerGateway') != records.gateway
            or answer.get('ownerProfile') != profile):
        raise AnswerRefused('owner_route_mismatch')
    # Resolve only a canonical board under the trusted base root, never a supplied
    # file, a worker's pinned DB, or the hub's replica database.
    board = answer.get('board')
    path = _board_db(board)
    # Validate the original even on receipt replay; source readback may be offline
    # only after the durable owner receipt already exists.
    if (original.get('id') != request_id or original.get('to_gw') != records.gateway
            or original.get('coll') != 'tasks' or original.get('action') != 'card-answer'
            or original.get('key') != f"{profile}/{board}/{answer.get('task')}"):
        raise AnswerRefused('owner_route_mismatch')
    with connect_closing(path) as conn:
        return apply(conn, answer, owner_gateway=records.gateway, owner_profile=profile, board=str(board), require_primary=True,
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    shape = {'owner': 2, 'source': 1}
    if not argv or argv[0] not in shape or len(argv) != shape[argv[0]] + 1:
        print(json.dumps({'ok': False, 'error': 'usage'}))
        return 64
    try:
        result = owner(*argv[1:]) if argv[0] == 'owner' else source(*argv[1:])
        out, code = {'ok': True, 'result': result}, 0
    except AnswerRefused as exc:
        out, code = {'ok': False, 'refused': str(exc) or 'refused'}, 2
    except (OSError, sqlite3.OperationalError):
        out, code = {'ok': False, 'retryable': True, 'error': 'unavailable'}, 3
    except Exception as exc:  # health: allow BLE001 -- top-level module protocol returns a fixed internal_error JSON envelope
        out, code = {'ok': False, 'error': 'internal_error', 'type': type(exc).__name__}, 1
    print(json.dumps(out, sort_keys=True, ensure_ascii=False, default=str))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
