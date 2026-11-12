"""Bounded operator-only priority operations on the supervisor CLI surface."""
from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_priority_executor as executor
from hermes_cli import kanban_priority_prepare as prepare
from hermes_cli.kanban_db_sync import sync_state
from hermes_cli.kanban_output import _err
from hermes_cli.kanban_supervisor_requests import _non_worker_refusal
from hermes_cli.kanban_sync_client import HubError, SyncUnavailable


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
        if action == 'migrate':
            from hermes_cli import kanban_priority_runner as runner
            code, result = runner.run(conn, args)
            print(json.dumps(result, sort_keys=True))
            return code
        handlers = {
            'publish': lambda: _publish(conn, args.writer),
            'prepare': lambda: _prepare_stage(conn, args),
            'stage': lambda: _prepare_stage(conn, args),
            'verify': lambda: _verify(conn, args, undo=False),
            'verify-restore': lambda: _verify(conn, args, undo=True),
            'apply': lambda: _apply(conn, args, undo=False),
            'restore': lambda: _apply(conn, args, undo=True),
        }
        if action not in handlers:
            raise ValueError('unknown priority operation')
        result = handlers[action]()
    except (ValueError, OSError, HubError, SyncUnavailable) as exc:
        return _err(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


def _apply(conn, args, *, undo: bool) -> dict:
    if not args.attachment_id or not args.digest:
        raise ValueError('--attachment-id and --digest required')
    if args.manifest or args.backup or args.exception:
        raise ValueError('apply/restore accept only the previously attached manifest')
    return executor.apply_attached(conn, args.attachment_id, args.digest, writer=args.writer,
                                   undo=undo, allow_legacy_rollback=args.allow_legacy_rollback)


def _manifest_bytes(path: str) -> bytes:
    # The caller's operator fence must run before consuming stdin.
    if path == '-':
        import sys
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _prepare_stage(conn, args) -> dict:
    refusal = _non_worker_refusal()
    if refusal:
        raise PermissionError(refusal)
    if not args.task or not args.backup:
        raise ValueError('--task and a fresh --backup path required')
    data = None
    if args.priority_operation == 'stage':
        if not args.manifest or args.exception:
            raise ValueError('stage requires --manifest and no --exception overrides')
        data = _manifest_bytes(args.manifest)
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
    return prepare.stage(conn, manifest, path=Path(args.backup), task_id=args.task,
                         writer=args.writer, original_bytes=data)


def _publish(conn, writer: str) -> dict:
    """Use the supported outbox push API, without a cap-aware snapshot pull."""
    refusal = _non_worker_refusal()
    if refusal:
        raise PermissionError(refusal)
    from hermes_cli import kanban_sync_client as sync
    identity = sync.load_identity()
    state = sync_state(conn) or {}
    if (identity.gateway != writer or state.get('gateway_id') != writer
            or state.get('mode') != 'primary' or not state.get('board_id')):
        raise ValueError('publication requires the configured enrolled primary writer')
    result = sync.push(conn, sync.Hub(identity), state['board_id'], writer, primary=True)
    counts = dict(conn.execute('SELECT state, COUNT(*) FROM kanban_sync_outbox GROUP BY state').fetchall())
    if counts.get('pending', 0) or counts.get('rejected', 0) or result.rejected or result.deferred:
        raise ValueError(f'publication incomplete: {counts}')
    return {'writer': writer, 'sent': result.sent, 'pending': 0, 'rejected': 0}


def _verify(conn, args, *, undo: bool) -> dict:
    import hashlib
    from hermes_cli import kanban_sync_client as sync
    from hermes_cli.kanban_priority_manifest import validate

    if not args.manifest or not args.digest:
        raise ValueError('verify requires --manifest and --digest')
    if args.backup or args.exception or args.attachment_id or args.allow_legacy_rollback or args.task:
        raise ValueError('verify accepts only manifest, digest and writer')
    data = Path(args.manifest).read_bytes()
    if hashlib.sha256(data).hexdigest() != args.digest:
        raise ValueError('manifest digest mismatch')
    manifest = validate(json.loads(data))
    identity = sync.load_identity()
    state = sync_state(conn) or {}
    if identity.gateway != args.writer or state.get('gateway_id') != args.writer:
        raise ValueError('configured gateway differs from explicit writer')
    if state.get('board_id') != manifest['board_id']:
        raise ValueError('manifest belongs to a different board')
    return prepare.verify(sync.Hub(identity), manifest, undo=undo)
