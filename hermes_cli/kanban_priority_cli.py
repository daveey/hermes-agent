"""Bounded operator-only priority operations on the supervisor CLI surface."""
from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_priority_executor as executor
from hermes_cli import kanban_priority_prepare as prepare
from hermes_cli.kanban_db_sync import sync_state
from hermes_cli.kanban_output import _err
from hermes_cli.kanban_supervisor_requests import _non_worker_refusal


def run(conn, args) -> int:
    """Do not turn a worker request, subprocess or delegated child into an operator."""
    refusal = _non_worker_refusal()
    if refusal:
        return _err(refusal)
    try:
        if args.drain or args.status:
            raise ValueError('priority operations cannot combine with drain/status')
        if not args.writer:
            raise ValueError('explicit --writer required')
        action = args.priority_operation
        if action in ('prepare', 'stage'):
            if not args.task or not args.backup:
                raise ValueError('--task and a fresh --backup path required')
            data = None
            if action == 'stage':
                if not args.manifest or args.exception:
                    raise ValueError('stage requires --manifest and no --exception overrides')
                data = Path(args.manifest).read_bytes()
                manifest = json.loads(data)
            else:
                if args.manifest:
                    raise ValueError('prepare reads canonical Board, not --manifest')
                from hermes_cli import kanban_sync_client as sync
                identity = sync.load_identity()
                state = sync_state(conn) or {}
                if identity.gateway != args.writer or state.get('gateway_id') != args.writer:
                    raise ValueError('configured gateway differs from explicit writer')
                if state.get('mode') != 'primary' or not state.get('board_id'):
                    raise ValueError('prepare requires an enrolled primary board')
                manifest = prepare.capture(sync.Hub(identity), board_id=state['board_id'], exceptions=args.exception)
            result = prepare.stage(conn, manifest, path=Path(args.backup), task_id=args.task,
                                   writer=args.writer, original_bytes=data)
        elif action in ('apply', 'restore'):
            if not args.attachment_id or not args.digest:
                raise ValueError('--attachment-id and --digest required')
            if args.manifest or args.backup or args.exception:
                raise ValueError('apply/restore accept only the previously attached manifest')
            result = executor.apply_attached(conn, args.attachment_id, args.digest, writer=args.writer,
                                             undo=action == 'restore',
                                             allow_legacy_rollback=args.allow_legacy_rollback)
        else:
            raise ValueError('unknown priority operation')
    except (ValueError, OSError) as exc:
        return _err(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0
